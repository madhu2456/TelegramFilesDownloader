"""Single-instance-per-user process lock engine with PID recycling & zombie safety."""
import atexit
import fcntl
import logging
import os
import signal
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

_active_lock_path: Path | None = None


def get_lock_file(session_path: Path | str) -> Path:
    """Derive lockfile path adjacent to session file: session/.{stem}.pid."""
    p = Path(session_path).resolve()
    return p.parent / f".{p.stem}.pid"


def is_pid_matching_app(pid: int) -> bool:
    """Check if a PID belongs to this project (not a recycled unrelated process)."""
    try:
        # Verify process UID matches current user
        stat = os.stat(f"/proc/{pid}")
        if stat.st_uid != os.getuid():
            return False
        # Verify cmdline contains project identifiers
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="replace").lower()
        markers = ("telegram", "tg-dl", "src.cli", "src.web", "run_web")
        return any(m in cmdline for m in markers)
    except (OSError, PermissionError, FileNotFoundError):
        return False


def is_zombie(pid: int) -> bool:
    """Check if a PID is in zombie state (Z) via /proc/{pid}/status."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        for line in status.splitlines():
            if line.startswith("State:"):
                return "\tZ" in line or "(zombie)" in line.lower()
    except (OSError, FileNotFoundError):
        pass
    return False


def _is_pid_alive(pid: int) -> bool:
    """Check if process is alive via kill(pid, 0)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive but owned by another user


def terminate_existing_process(pid: int, timeout: float = 2.0) -> bool:
    """Gracefully terminate a process: SIGTERM -> poll -> SIGKILL."""
    if is_zombie(pid):
        log.info("Previous instance (PID %d) is a zombie; skipping signal.", pid)
        return True

    # Stage 1: SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
        log.info("Sent SIGTERM to previous instance (PID %d).", pid)
    except (ProcessLookupError, PermissionError):
        return True

    # Poll for exit
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            log.info("Previous instance (PID %d) terminated gracefully.", pid)
            return True
        time.sleep(0.1)

    # Stage 2: SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
        log.info("Sent SIGKILL to previous instance (PID %d).", pid)
    except (ProcessLookupError, PermissionError):
        return True

    kill_deadline = time.monotonic() + 1.0
    while time.monotonic() < kill_deadline:
        if not _is_pid_alive(pid):
            log.info("Previous instance (PID %d) killed.", pid)
            return True
        time.sleep(0.1)

    log.warning("Previous instance (PID %d) could not be terminated.", pid)
    return False


def acquire_instance_lock(session_path: Path | str) -> Path:
    """Acquire single-instance lock for the given session, terminating any previous instance."""
    global _active_lock_path
    lock_path = get_lock_file(session_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open/create lockfile and acquire exclusive advisory lock
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)

        # Read existing PID
        content = os.pread(fd, 64, 0).decode("utf-8", errors="replace").strip()
        if content:
            try:
                old_pid = int(content)
                if old_pid > 0 and old_pid != os.getpid() and _is_pid_alive(old_pid):
                    if is_pid_matching_app(old_pid):
                        log.info("Terminating previous instance (PID %d) for session %s.", old_pid, session_path)
                        terminate_existing_process(old_pid)
                    else:
                        log.info("PID %d is not a matching app process; treating lock as stale.", old_pid)
            except ValueError:
                pass

        # Write current PID
        pid_bytes = str(os.getpid()).encode("utf-8")
        os.ftruncate(fd, 0)
        os.pwrite(fd, pid_bytes, 0)
        os.fchmod(fd, 0o600)

    finally:
        # Release advisory lock but keep file
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    _active_lock_path = lock_path

    # Register cleanup handlers
    atexit.register(cleanup_instance_lock)

    # Install signal handlers that chain to previous handlers
    prev_sigterm = signal.getsignal(signal.SIGTERM)
    prev_sigint = signal.getsignal(signal.SIGINT)

    def _handle_term(signum, frame):
        cleanup_instance_lock()
        if callable(prev_sigterm) and prev_sigterm not in (signal.SIG_DFL, signal.SIG_IGN):
            prev_sigterm(signum, frame)
        sys.exit(128 + signum)

    def _handle_int(signum, frame):
        cleanup_instance_lock()
        if callable(prev_sigint) and prev_sigint not in (signal.SIG_DFL, signal.SIG_IGN):
            prev_sigint(signum, frame)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_int)

    log.info("Instance lock acquired: %s (PID %d)", lock_path, os.getpid())
    return lock_path


def cleanup_instance_lock() -> None:
    """Remove lockfile only if it still contains our PID (ownership guard)."""
    global _active_lock_path
    if _active_lock_path is None:
        return
    try:
        content = _active_lock_path.read_text().strip()
        if content == str(os.getpid()):
            _active_lock_path.unlink(missing_ok=True)
            log.info("Instance lock released: %s", _active_lock_path)
    except (OSError, FileNotFoundError):
        pass
    _active_lock_path = None
