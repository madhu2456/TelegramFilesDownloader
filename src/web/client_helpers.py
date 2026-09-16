"""Shared Telethon client and multi-tenant session helper functions for the web dashboard."""

import asyncio
import inspect
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.websockets import WebSocket

from src.web.security import verify_token
from src.web.session_security import get_session_id_from_request


async def _ensure_connected(client: Any) -> None:
    """Ensure the Telegram client is connected, awaiting is_connected() if it is a coroutine."""
    if client is None:
        return
    conn = client.is_connected()
    if inspect.iscoroutine(conn):
        conn = await conn
    if not conn:
        await client.connect()


async def _resolve_peer_robust(client: Any, chat_id: Any) -> Any:
    """Resolve an integer or string chat_id into a valid Telethon InputPeer or entity."""
    if client is None:
        return chat_id
    try:
        return await client.get_input_entity(chat_id)
    except Exception:
        pass

    try:
        from telethon import utils
        from telethon.tl.types import PeerChannel, PeerChat, PeerUser

        val = int(chat_id)
        candidates: list[Any] = []
        if val > 0:
            candidates.extend(
                [
                    int(f"-100{val}"),
                    PeerChannel(val),
                    PeerChat(val),
                    PeerUser(val),
                ]
            )
        else:
            candidates.extend(
                [
                    val,
                    utils.resolve_id(val)[0],
                ]
            )

        for cand in candidates:
            try:
                return await client.get_input_entity(cand)
            except Exception:
                continue
    except Exception:
        pass

    try:
        return await client.get_entity(chat_id)
    except Exception:
        pass

    return chat_id


def extract_master_token(request: Request | WebSocket) -> str | None:
    """Extract master access token from headers or query parameters with Bearer support.
    Returns stripped non-empty token string or None."""
    auth_header = request.headers.get("authorization")
    bearer = auth_header.replace("Bearer ", "").strip() if auth_header else None
    for cand in (
        request.headers.get("x-auth-token"),
        request.query_params.get("token"),
        bearer,
    ):
        if cand and str(cand).strip():
            return str(cand).strip()
    return None


async def get_session_client(request: Request, allow_jit: bool = False) -> Any:
    """Retrieve the TelegramClient with strict Master-Token Precedence.

    1. If a valid master token is provided, always return app.state.tg_client (Admin/Test mode).
    2. Otherwise, check for visitor session cookie and fetch from SessionManager.
    3. Fall back to app.state.tg_client if session cookie is absent.
    """
    token = extract_master_token(request)
    if token and verify_token(token):
        return getattr(request.app.state, "tg_client", None)

    session_id = get_session_id_from_request(request)
    if session_id:
        sm = getattr(request.app.state, "session_manager", None)
        if sm:
            tenant = await sm.get_or_create_tenant(session_id, allow_jit=allow_jit)
            if tenant and tenant.client:
                return tenant.client

    return getattr(request.app.state, "tg_client", None)


def get_session_job_manager(request: Request) -> Any:
    """Retrieve the JobManager with Master-Token Precedence."""
    token = extract_master_token(request)
    if token and verify_token(token):
        return getattr(request.app.state, "job_manager", None)

    session_id = get_session_id_from_request(request)
    if session_id:
        sm = getattr(request.app.state, "session_manager", None)
        if sm and session_id in sm._sessions:
            return sm._sessions[session_id].job_manager

    return getattr(request.app.state, "job_manager", None)


def get_session_out_dir(request: Request) -> Path:
    """Retrieve the tenant output directory with Master-Token Precedence."""
    token = extract_master_token(request)
    if token and verify_token(token):
        return Path(getattr(request.app.state, "out_dir", "out"))

    session_id = get_session_id_from_request(request)
    if session_id:
        sm = getattr(request.app.state, "session_manager", None)
        if sm and session_id in sm._sessions:
            return sm._sessions[session_id].out_dir

    return Path(getattr(request.app.state, "out_dir", "out"))


def get_session_lock(request: Request) -> asyncio.Lock:
    """Retrieve the per-session lock or fallback lock."""
    session_id = get_session_id_from_request(request)
    if session_id:
        sm = getattr(request.app.state, "session_manager", None)
        if sm and session_id in sm._sessions:
            return sm._sessions[session_id].lock

    lock = getattr(request.app.state, "default_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.default_lock = lock
    return lock


def get_tenant_session(request: Request) -> Any:
    """Retrieve the TenantSession if active in SessionManager."""
    session_id = get_session_id_from_request(request)
    if session_id:
        sm = getattr(request.app.state, "session_manager", None)
        if sm and session_id in sm._sessions:
            return sm._sessions[session_id]
    return None
