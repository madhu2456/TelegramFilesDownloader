"""Decoupled JobManager with singleton mutex, circular logs, and detached asyncio.Task."""
import asyncio
import collections
from datetime import datetime, timezone
import logging
import time
import uuid

log = logging.getLogger(__name__)

class JobConflictError(Exception):
    """Raised when an attempt is made to start a job while another job is already running."""
    pass

class JobManager:
    """Thread-safe and async-safe manager for download executions."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._is_running: bool = False
        self._active_job_id: str | None = None
        self._active_task: asyncio.Task | None = None
        self._cancel_event: asyncio.Event = asyncio.Event()
        self._subscribers: set[asyncio.Queue] = set()
        self._logs: collections.deque = collections.deque(maxlen=1000)
        self._snapshot: dict = {
            "status": "idle",
            "job_id": None,
            "target": None,
            "progress": 0.0,
            "total_msgs": 0,
            "downloaded_msgs": 0,
            "skipped_msgs": 0,
            "speed_mbps": 0.0,
            "eta_seconds": None,
            "current_file": None,
            "flood_wait_seconds": None,
            "started_at": None,
            "finished_at": None,
            "bytes_total": 0,
        }

    def is_running(self) -> bool:
        return self._is_running

    def get_snapshot(self) -> dict:
        snap = dict(self._snapshot)
        if self._is_running:
            snap["status"] = "running"
            snap["job_id"] = self._active_job_id
        return snap

    def get_recent_logs(self) -> list[str]:
        return list(self._logs)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def add_log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self._logs.append(line)
        self._broadcast({"type": "LOG", "message": line})

    def set_flood_wait(self, seconds: int) -> None:
        self._snapshot["flood_wait_seconds"] = seconds
        self.add_log(f"Rate limited by Telegram: FloodWait {seconds}s")
        self._broadcast({"type": "FLOOD_WAIT", "seconds": seconds, "state": self.get_snapshot()})

    def _broadcast(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except (asyncio.QueueFull, Exception):
                pass

    async def start_job(self, client, target, opts, out, conn) -> str:
        async with self._lock:
            if self._is_running:
                raise JobConflictError("A download job is already running")
            self._is_running = True
            self._active_job_id = f"job-{uuid.uuid4().hex[:8]}"
            self._cancel_event.clear()
            self._snapshot = {
                "status": "running",
                "job_id": self._active_job_id,
                "target": str(getattr(target, "value", target)),
                "progress": 0.0,
                "total_msgs": int(getattr(opts, "limit", 500) or 500),
                "downloaded_msgs": 0,
                "skipped_msgs": 0,
                "speed_mbps": 0.0,
                "eta_seconds": None,
                "current_file": None,
                "flood_wait_seconds": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "bytes_total": 0,
            }
            self.add_log(f"Job {self._active_job_id} initiated for target {self._snapshot['target']}")
            self._active_task = asyncio.create_task(
                self._run_job(self._active_job_id, client, target, opts, out, conn)
            )
            return self._active_job_id

    def cancel_job(self) -> bool:
        if self._is_running:
            self._cancel_event.set()
            self.add_log("Cancellation requested by user.")
            self._broadcast({"type": "CANCEL_REQUESTED", "job_id": self._active_job_id})
            return True
        return False

    async def _run_job(self, job_id: str, client, target, opts, out, conn) -> None:
        t0 = time.monotonic()
        try:
            from src.downloader import download_chat
            setattr(opts, "cancel_event", self._cancel_event)

            res = await download_chat(client, target, opts, out, conn)
            status = "cancelled" if self._cancel_event.is_set() else "completed"
            self._snapshot["status"] = status
            self._snapshot["progress"] = 100.0 if status == "completed" else self._snapshot["progress"]
            self._snapshot["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._snapshot["downloaded_msgs"] = res.get("done", 0)
            self._snapshot["skipped_msgs"] = res.get("skipped", 0)
            self._snapshot["bytes_total"] = res.get("bytes", 0)
            elapsed = round(time.monotonic() - t0, 1)
            self.add_log(f"Job {job_id} {status} in {elapsed}s: done={res.get('done', 0)}, skipped={res.get('skipped', 0)}, bytes={res.get('bytes', 0)}")
            self._broadcast({"type": "COMPLETED", "state": self.get_snapshot(), "result": res})
        except Exception as exc:
            self._snapshot["status"] = "failed"
            self._snapshot["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.add_log(f"Job {job_id} error: {exc}")
            self._broadcast({"type": "FAILED", "state": self.get_snapshot(), "error": str(exc)})
        finally:
            async with self._lock:
                self._is_running = False
                self._active_job_id = None
                self._active_task = None
