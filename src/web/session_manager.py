"""Bounded LRU client pool, zero-JIT session jailing, and idle session sweeper."""

import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger("televault.session_manager")


@dataclass
class TenantSession:
    """Isolated session state for an authenticated visitor tenant."""

    session_id: str
    client: Any  # TelegramClient or None
    job_manager: Any  # JobManager
    out_dir: Path
    lock: asyncio.Lock
    last_accessed: float
    phone_code_hash: str | None = None
    active_qr: Any = None


class SessionCapacityExceededError(HTTPException):
    """Raised when all active client slots are occupied by running downloads."""

    def __init__(self, detail: str = "Server download capacity reached. Please try again later."):
        super().__init__(status_code=503, detail=detail, headers={"Retry-After": "60"})


class SessionManager:
    """Bounded LRU pool managing per-visitor TelegramClient instances and download sandboxes."""

    MAX_ACTIVE_CLIENTS: int = 25
    IDLE_TTL_SECONDS: float = 900.0

    def __init__(self, cfg: Any = None, base_out_dir: Path | str = "out") -> None:
        self.cfg = cfg
        self.base_out_dir = Path(base_out_dir)
        self._sessions: OrderedDict[str, TenantSession] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get_or_create_tenant(self, session_id: str, allow_jit: bool = False) -> TenantSession | None:
        """Fetch an active tenant session or instantiate one if valid on disk or during explicit auth."""
        async with self._lock:
            if session_id in self._sessions:
                t = self._sessions[session_id]
                t.last_accessed = time.time()
                self._sessions.move_to_end(session_id)
                return t

            from src.web.session_security import get_canonical_out_dir, get_canonical_session_path

            sess_path = get_canonical_session_path(session_id)
            if not sess_path.exists() and not allow_jit:
                return None

            if len(self._sessions) >= self.MAX_ACTIVE_CLIENTS:
                evicted = False
                for sid, t in list(self._sessions.items()):
                    if not t.job_manager.is_running():
                        self._sessions.pop(sid)
                        if t.client:
                            with suppress(Exception):
                                await t.client.disconnect()
                        evicted = True
                        break
                if not evicted:
                    raise SessionCapacityExceededError("Server download capacity reached. Please try again later.")

            out_dir = get_canonical_out_dir(session_id, base_out_dir=self.base_out_dir)
            from src.web.job_manager import JobManager

            job_manager = JobManager()
            client = None
            if self.cfg and getattr(self.cfg, "api_id", None) and getattr(self.cfg, "api_hash", None):
                from telethon import TelegramClient

                from src.store import harden_sqlite_session

                harden_sqlite_session(sess_path)
                client = TelegramClient(str(sess_path), int(self.cfg.api_id), str(self.cfg.api_hash))

            tenant = TenantSession(
                session_id=session_id,
                client=client,
                job_manager=job_manager,
                out_dir=out_dir,
                lock=asyncio.Lock(),
                last_accessed=time.time(),
            )
            self._sessions[session_id] = tenant
            return tenant

    async def cleanup_idle_sessions(self) -> int:
        """Sweep and disconnect idle tenant sessions that exceed the TTL and are not downloading."""
        now = time.time()
        to_disconnect = []
        async with self._lock:
            for sid, t in list(self._sessions.items()):
                if (now - t.last_accessed > self.IDLE_TTL_SECONDS) and not t.job_manager.is_running():
                    self._sessions.pop(sid, None)
                    if t.client:
                        to_disconnect.append(t.client)
        for client in to_disconnect:
            with suppress(Exception):
                await client.disconnect()
        return len(to_disconnect)

    async def logout_tenant(self, session_id: str) -> bool:
        """Log out and disconnect a tenant's TelegramClient, deleting local session file."""
        from src.web.session_security import get_canonical_session_path

        async with self._lock:
            t = self._sessions.pop(session_id, None)
        if t and t.client:
            with suppress(Exception):
                await t.client.log_out()
            with suppress(Exception):
                await t.client.disconnect()
        with suppress(Exception):
            sess_path = get_canonical_session_path(session_id)
            sess_path.unlink(missing_ok=True)
        return True

    async def close_all(self) -> None:
        """Gracefully disconnect all active tenant clients in the pool."""
        async with self._lock:
            for t in self._sessions.values():
                if t.client:
                    with suppress(Exception):
                        await t.client.disconnect()
            self._sessions.clear()
