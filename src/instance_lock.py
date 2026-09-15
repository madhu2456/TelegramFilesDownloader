"""Single-instance-per-user process lock engine with PID recycling & zombie safety."""
import atexit
import fcntl
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_active_lock_path: Path | None = None
_active_lock_fd: int | None = None


def get_lock_file(session_path: Path | str) -> Path:
    """Derive lockfile path adjacent to session file: session/.{stem}.pid."""
    p = Path(session_path).resolve()
    return p.parent / f".{p.stem}.pid"


def is_pid_matching_app(pid: int) -> bool:
    """Check if a PID belongs to this project with Linux /proc and portable fallback."""
    markers = ("telegram", "tg-dl", "src.cli", "src.web", "run_web", "televault")
    proc_path = Path(f"/proc/{pid}")
    if proc_path.exists():
        try:
            stat = os.stat(f"/proc/{pid}")
            if stat.st_uid != os.getuid():
                return False
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="replace").lower()
            if any(m in cmdline for m in markers):
                return True
            if "pytest" in cmdline:
                try:
                    cwd = os.readlink(f"/proc/{pid}/cwd").lower()
                    if any(m in cwd for m in ("telegram", "tg-dl", "televault")):
                        return True
                except (OSError, PermissionError, FileNotFoundError):
                    pass
            return False
        except (OSError, PermissionError, FileNotFoundError):
            return False

    # Portable fallback for macOS, BSD, or containers where /proc is unmounted
    import subprocess
    try:
        res = subprocess.run(["ps", "-p", str(pid), "-o", "uid=", "-o", "args="], capture_output=True, text=True, timeout=2.0, check=False)
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split(None, 1)
            if len(parts) >= 1 and parts[0].isdigit() and int(parts[0]) != os.getuid():
                return False
            cmd = res.stdout.lower()
            if any(m in cmd for m in markers):
                return True
            if "pytest" in cmd:
                try:
                    cwd = os.getcwd().lower()
                    if any(m in cwd for m in ("telegram", "tg-dl", "televault")):
                        return True
                except Exception:
                    pass
    except Exception:
        pass
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
    if pid <= 0:
        return False
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
    global _active_lock_path, _active_lock_fd
    lock_path = get_lock_file(session_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + 4.0
    fd = None
    while time.monotonic() < deadline:
        candidate_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            fcntl.flock(candidate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st_fd = os.fstat(candidate_fd)
            try:
                st_path = lock_path.stat()
                if (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
                    os.close(candidate_fd)
                    time.sleep(0.05)
                    continue
            except FileNotFoundError:
                os.close(candidate_fd)
                time.sleep(0.05)
                continue

            fd = candidate_fd
            break
        except (BlockingIOError, OSError):
            try:
                content = Path(lock_path).read_text(encoding="utf-8").strip()
                if content.isdigit():
                    old_pid = int(content)
                    if old_pid > 0 and old_pid != os.getpid() and _is_pid_alive(old_pid):
                        if is_pid_matching_app(old_pid):
                            log.info("Terminating previous instance (PID %d) for session %s.", old_pid, session_path)
                            terminate_existing_process(old_pid)
                        else:
                            log.info("PID %d is not a matching app process; treating lock as stale.", old_pid)
            except Exception:
                pass
            os.close(candidate_fd)
            time.sleep(0.1)

    if fd is None:
        raise TimeoutError(f"Failed to acquire instance lock on {lock_path} within timeout")

    # Write current PID
    try:
        pid_bytes = str(os.getpid()).encode("utf-8")
        os.ftruncate(fd, 0)
        os.pwrite(fd, pid_bytes, 0)
        os.fchmod(fd, 0o600)
    except Exception:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        raise

    _active_lock_fd = fd
    _active_lock_path = lock_path

    # Register cleanup handlers
    atexit.register(cleanup_instance_lock)

    # Install signal handlers only when in the main thread
    if threading.current_thread() is threading.main_thread():
        try:
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
        except (ValueError, AttributeError):
            pass

    log.info("Instance lock acquired: %s (PID %d)", lock_path, os.getpid())
    return lock_path


def cleanup_instance_lock() -> None:
    """Remove lockfile and release flock descriptor cleanly without deadlock."""
    global _active_lock_path, _active_lock_fd
    lock_path = _active_lock_path
    fd = _active_lock_fd
    _active_lock_path = None
    _active_lock_fd = None

    if fd is not None:
        try:
            if lock_path is not None:
                try:
                    st_fd = os.fstat(fd)
                    st_path = lock_path.stat()
                    if (st_fd.st_dev, st_fd.st_ino) == (st_path.st_dev, st_path.st_ino):
                        lock_path.unlink(missing_ok=True)
                        log.info("Instance lock released: %s", lock_path)
                except FileNotFoundError:
                    pass
                except Exception as e:
                    log.warning("Failed to verify lockfile inode before unlinking %s: %s", lock_path, e)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
