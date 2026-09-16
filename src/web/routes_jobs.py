"""API routes for job control, state snapshot, and WebSocket streaming."""

import asyncio

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.downloader import DownloadOpts
from src.resolver import resolve_target
from src.store import init_db
from src.web.client_helpers import (
    _ensure_connected,
    extract_master_token,
    get_session_client,
    get_session_job_manager,
    get_session_out_dir,
)
from src.web.job_manager import JobConflictError
from src.web.security import verify_origin, verify_token
from src.web.session_security import SESSION_COOKIE_NAME, validate_session_id

router = APIRouter()


class DownloadStartRequest(BaseModel):
    target: str
    limit: int | None = 500
    no_limit: bool = False
    filter: str | None = None
    after: str | None = None
    before: str | None = None
    from_user: str | None = None
    search: str | None = None
    ids: list[int] | None = None
    dry_run: bool = False
    resume: bool = False
    reverse: bool = True
    takeout: bool = False
    min_id: int | None = None
    sync: bool = False
    join: bool = False


@router.post("/api/download/start")
async def start_download(req: DownloadStartRequest, request: Request):
    jm = get_session_job_manager(request)
    if not jm:
        raise HTTPException(status_code=500, detail="Job manager not initialized")
    if jm.is_running():
        jm.add_log(f"Auto-terminating active job {jm._active_job_id} for new request.")
        await jm.cancel_job_async(timeout=5.0)

    client = await get_session_client(request, allow_jit=False)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")

    await _ensure_connected(client)

    try:
        resolved = await resolve_target(client, req.target, join=req.join)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Target resolution failed: {e}")

    out_dir = get_session_out_dir(request)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = init_db(out_dir / "manifest.db")

    raw_lim = None if req.no_limit else req.limit
    eff_lim = None if (raw_lim is None or int(raw_lim) <= 0) else int(raw_lim)
    opts = DownloadOpts(
        limit=eff_lim,
        filter=req.filter,
        after=req.after,
        before=req.before,
        from_user=req.from_user,
        search=req.search,
        ids=req.ids,
        dry_run=req.dry_run,
        resume=req.resume,
        reverse=req.reverse,
        takeout=req.takeout,
        min_id=req.min_id,
        sync=req.sync,
    )

    try:
        job_id = await jm.start_job(client, resolved, opts, out_dir, db)
        return {"status": "started", "job_id": job_id}
    except JobConflictError:
        try:
            db.close()
        except Exception:
            pass
        return JSONResponse(
            status_code=409,
            content={"error": "CONFLICT", "detail": "A download job is already running", "job_id": jm._active_job_id},
        )
    except Exception:
        try:
            db.close()
        except Exception:
            pass
        raise


@router.post("/api/download/cancel")
async def cancel_download(request: Request):
    jm = get_session_job_manager(request)
    cancelled = jm.cancel_job() if jm else False
    return {"status": "cancelling" if cancelled else "no_active_job"}


@router.get("/api/download/state")
async def get_download_state(request: Request):
    jm = get_session_job_manager(request)
    if not jm:
        return {"snapshot": {}, "logs": []}
    return {"snapshot": jm.get_snapshot(), "logs": jm.get_recent_logs()}


@router.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    token = extract_master_token(websocket)
    raw_sid = websocket.cookies.get(SESSION_COOKIE_NAME)
    session_id = raw_sid if validate_session_id(raw_sid) else None
    is_authed = False
    jm = None

    if token and verify_token(token):
        is_authed = True
        jm = getattr(websocket.app.state, "job_manager", None)
    elif session_id:
        sm = getattr(websocket.app.state, "session_manager", None)
        if sm:
            tenant = await sm.get_or_create_tenant(session_id, allow_jit=False)
            if tenant:
                is_authed = True
                jm = tenant.job_manager

    if not is_authed or jm is None:
        await websocket.close(code=1008, reason="Policy Violation: Authentication required")
        return

    origin = websocket.headers.get("origin")
    port = websocket.url.port or 8000
    if not verify_origin(origin, port):
        await websocket.close(code=1008, reason="Forbidden origin")
        return

    await websocket.accept()
    q = jm.subscribe()
    try:
        await websocket.send_json({"type": "INIT_STATE", "state": jm.get_snapshot(), "logs": jm.get_recent_logs()})
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except (WebSocketDisconnect, ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        jm.unsubscribe(q)

