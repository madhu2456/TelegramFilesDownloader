"""FastAPI application factory with ephemeral security, origin validation, and static serving."""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.cli import get_client
from src.config import Config, load_config, scrub_for_log
from src.web.job_manager import JobManager
from src.web.routes_jobs import router as jobs_router
from src.web.routes_media import router as media_router
from src.web.routes_tg import router as tg_router
from src.web.security import mask_phone, verify_origin, verify_token

def create_app(cfg: Config | None = None) -> FastAPI:
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = None

    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if cfg and not getattr(app.state, "tg_client", None):
            try:
                app.state.tg_client = get_client(cfg)
            except Exception:
                app.state.tg_client = None
        yield
        if getattr(app.state, "tg_client", None):
            try:
                if app.state.tg_client.is_connected():
                    await app.state.tg_client.disconnect()
            except Exception:
                pass

    app = FastAPI(title="TeleVault Web Dashboard", lifespan=lifespan)
    app.state.job_manager = JobManager()
    app.state.out_dir = "out"
    app.state.config = cfg
    app.state.tg_client = None

    # Security & CSRF Middleware
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path

        # Origin validation for state-changing requests
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            port = request.url.port or 8000
            if not verify_origin(origin, port):
                return JSONResponse(status_code=403, content={"error": "FORBIDDEN", "detail": "Cross-origin request rejected"})

        # Token validation on /api/* endpoints
        if path.startswith("/api/"):
            token = (
                request.headers.get("x-auth-token")
                or request.query_params.get("token")
                or (request.headers.get("authorization", "").replace("Bearer ", "").strip() if request.headers.get("authorization") else None)
            )
            if not verify_token(token):
                return JSONResponse(status_code=401, content={"error": "UNAUTHORIZED", "detail": "Invalid or missing token"})

        return await call_next(request)

    app.include_router(jobs_router)
    app.include_router(tg_router)
    app.include_router(media_router)

    app.mount("/static", StaticFiles(directory=str(static_dir), html=True), name="static")

    @app.get("/")
    async def root():
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"status": "ok", "app": "TeleVault Web Dashboard"}

    @app.get("/api/status")
    async def status(request: Request):
        c = getattr(request.app.state, "config", None)
        session_path = getattr(c, "session_path", Path("session/telegram.session")) if c else Path("session/telegram.session")
        phone_masked = mask_phone(getattr(c, "phone", "")) if c else ""
        out_dir = str(getattr(request.app.state, "out_dir", "out"))
        scrubbed = scrub_for_log({"out_dir": out_dir, "session_exists": session_path.exists()})
        return {
            "status": "ok",
            "phone_masked": phone_masked,
            "session_exists": scrubbed["session_exists"],
            "out_dir": scrubbed["out_dir"],
        }

    return app
