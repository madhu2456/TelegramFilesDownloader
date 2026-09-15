"""Tests for single-instance-per-user process lock engine."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.instance_lock import (
    acquire_instance_lock,
    cleanup_instance_lock,
    get_lock_file,
    is_pid_matching_app,
    terminate_existing_process,
)


def test_lock_creation_and_cleanup(tmp_path: Path):
    session = tmp_path / "test.session"
    session.touch()

    lock = acquire_instance_lock(session)
    assert lock.exists()
    content = lock.read_text().strip()
    assert content == str(os.getpid())

    # Verify permissions are 0o600
    mode = lock.stat().st_mode & 0o777
    assert mode == 0o600

    cleanup_instance_lock()
    assert not lock.exists()


def test_get_lock_file_derives_correct_path(tmp_path: Path):
    p1 = get_lock_file(tmp_path / "userA.session")
    p2 = get_lock_file(tmp_path / "userB.session")
    assert p1 != p2
    assert p1.name == ".userA.pid"
    assert p2.name == ".userB.pid"
    assert p1.parent == tmp_path
    assert p2.parent == tmp_path


def test_multi_user_session_isolation(tmp_path: Path):
    session_a = tmp_path / "userA.session"
    session_b = tmp_path / "userB.session"
    session_a.touch()
    session_b.touch()

    lock_a = get_lock_file(session_a)
    lock_b = get_lock_file(session_b)
    assert lock_a != lock_b


def test_is_pid_matching_app_current_process():
    # Current process should match since it's running Python with project code
    result = is_pid_matching_app(os.getpid())
    assert result is True


def test_skip_recycled_pid_mismatched_cmdline(tmp_path: Path, monkeypatch):
    """If a PID's cmdline doesn't match project markers, it should not be terminated."""
    session = tmp_path / "test_recycle.session"
    session.touch()
    lock_path = get_lock_file(session)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Write a fake PID that matches current process PID + 99999 (likely non-existent)
    fake_pid = 99999

    # Mock is_pid_matching_app to return False (simulating recycled PID)
    killed_pids = []
    original_kill = os.kill

    def guarded_kill(pid, sig):
        if pid == fake_pid:
            killed_pids.append((pid, sig))
            raise ProcessLookupError("No such process")
        return original_kill(pid, sig)

    with patch("src.instance_lock.is_pid_matching_app", return_value=False), \
         patch("src.instance_lock._is_pid_alive", return_value=True), \
         patch("os.kill", side_effect=guarded_kill):
        lock_path.write_text(str(fake_pid))
        os.chmod(str(lock_path), 0o600)
        acquire_instance_lock(session)

    # Verify SIGTERM was never sent (no kill calls for fake_pid)
    sigterm_calls = [p for p, s in killed_pids if s != 0]
    assert len(sigterm_calls) == 0

    # Lock should now have current PID
    assert lock_path.read_text().strip() == str(os.getpid())
    cleanup_instance_lock()


def test_skip_zombie_process(tmp_path: Path, monkeypatch):
    """Zombie processes should be detected and signals skipped."""
    with patch("src.instance_lock.is_zombie", return_value=True):
        result = terminate_existing_process(99999)
        assert result is True


def test_dynamic_port_allocation():
    """Verify find_available_port auto-increments when port is in use."""
    import socket

    from src.web.__main__ import find_available_port

    # Bind port 8000 to simulate another user's dashboard (or reuse existing in-use state)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            blocker.bind(("127.0.0.1", 8000))
            blocker.listen(1)
        except OSError:
            pass  # Port 8000 already occupied by active server, verifying auto-increment

        port = find_available_port(8000, 50)
        assert port > 8000
        assert port <= 8050
    finally:
        blocker.close()


def test_acquire_lock_with_dead_pid(tmp_path: Path):
    session = tmp_path / "dead_pid.session"
    session.touch()
    lock_path = get_lock_file(session)
    lock_path.write_text("99999999")

    lock = acquire_instance_lock(session)
    assert lock.exists()
    assert lock.read_text().strip() == str(os.getpid())
    cleanup_instance_lock()
    assert not lock.exists()


def test_cleanup_instance_lock_idempotent():
    cleanup_instance_lock()
    cleanup_instance_lock()


def test_terminate_existing_process_sigkill_escalation(monkeypatch):
    """If process does not terminate after SIGTERM, SIGKILL is issued."""
    import signal
    signals_sent = []

    def mock_kill(pid, sig):
        signals_sent.append(sig)

    monkeypatch.setattr("os.kill", mock_kill)

    def mock_is_alive(pid):
        # Stays alive until SIGKILL is received
        return signal.SIGKILL not in signals_sent

    monkeypatch.setattr("src.instance_lock._is_pid_alive", mock_is_alive)
    monkeypatch.setattr("src.instance_lock.is_zombie", lambda pid: False)
    monkeypatch.setattr("time.sleep", lambda s: None)

    curr_time = [100.0]

    def mock_monotonic():
        curr_time[0] += 0.05
        return curr_time[0]

    monkeypatch.setattr("time.monotonic", mock_monotonic)

    res = terminate_existing_process(99999, timeout=0.1)
    assert res is True
    assert signal.SIGTERM in signals_sent
    assert signal.SIGKILL in signals_sent


def test_cleanup_instance_lock_does_not_unlink_on_stat_exception(tmp_path: Path):
    session = tmp_path / "stat_err.session"
    session.touch()
    lock = acquire_instance_lock(session)
    assert lock.exists()

    with patch.object(Path, "stat", side_effect=OSError("Disk error")):
        cleanup_instance_lock()

    # Active lock should NOT have been unlinked unconditionally
    assert lock.exists()
    lock.unlink(missing_ok=True)


def test_is_pid_matching_app_returns_false_on_cmdline_failure():
    with patch("pathlib.Path.exists", return_value=False), \
         patch("subprocess.run", side_effect=Exception("ps failed")), \
         patch("src.instance_lock._is_pid_alive", return_value=True):
        assert is_pid_matching_app(12345) is False


def test_acquire_instance_lock_timeout_raises_timeout_error(tmp_path: Path):
    session = tmp_path / "timeout_test.session"
    session.touch()

    with patch("fcntl.flock", side_effect=BlockingIOError), \
         patch("time.sleep", return_value=None), \
         patch("time.time", side_effect=[0.0, 0.0, 10.0]):
        with pytest.raises(TimeoutError) as exc_info:
            acquire_instance_lock(session)
        assert "Failed to acquire instance lock" in str(exc_info.value)


