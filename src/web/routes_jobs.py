"""API routes for job control, state snapshot, and WebSocket streaming."""
import asyncio
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from src.downloader import DownloadOpts
from src.resolver import resolve_target
from src.store import init_db
from src.web.job_manager import JobConflictError, JobManager
from src.web.security import verify_origin, verify_token

from fastapi.responses import JSONResponse

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
    jm: JobManager = request.app.state.job_manager
    if jm.is_running():
        jm.add_log(f"Auto-terminating active job {jm._active_job_id} for new request.")
        await jm.cancel_job_async(timeout=5.0)

    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")

    if not client.is_connected():
        await client.connect()

    out_dir = Path(getattr(request.app.state, "out_dir", "out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    db = init_db(out_dir / "manifest.db")

    try:
        resolved = await resolve_target(client, req.target, join=req.join)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Target resolution failed: {e}")

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
        return JSONResponse(
            status_code=409,
            content={"error": "CONFLICT", "detail": "A download job is already running", "job_id": jm._active_job_id}
        )

@router.post("/api/download/cancel")
async def cancel_download(request: Request):
    jm: JobManager = request.app.state.job_manager
    cancelled = jm.cancel_job()
    return {"status": "cancelling" if cancelled else "no_active_job"}

@router.get("/api/download/state")
async def get_download_state(request: Request):
    jm: JobManager = request.app.state.job_manager
    return {"snapshot": jm.get_snapshot(), "logs": jm.get_recent_logs()}

@router.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    token = websocket.query_params.get("token") or websocket.headers.get("x-auth-token")
    if not verify_token(token):
        try:
            await websocket.accept()
            await websocket.send_json({
                "type": "SESSION_EXPIRED",
                "detail": "Session token invalid or expired"
            })
            await websocket.close(code=1008, reason="Policy Violation: Invalid token")
        except Exception:
            pass
        return

    origin = websocket.headers.get("origin")
    port = websocket.url.port or 8000
    if not verify_origin(origin, port):
        try:
            await websocket.accept()
            await websocket.send_json({
                "type": "ORIGIN_FORBIDDEN",
                "detail": "Origin not allowed"
            })
            await websocket.close(code=1008, reason="Policy Violation: Invalid origin")
        except Exception:
            pass
        return

    await websocket.accept()
    jm: JobManager = websocket.app.state.job_manager
    q = jm.subscribe()
    try:
        await websocket.send_json({
            "type": "INIT_STATE",
            "state": jm.get_snapshot(),
            "logs": jm.get_recent_logs()
        })
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except (WebSocketDisconnect, ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        jm.unsubscribe(q)
