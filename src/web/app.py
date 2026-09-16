"""FastAPI application factory with ephemeral security, origin validation, and static serving."""

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.config import Config, load_config, scrub_for_log
from src.web.client_helpers import extract_master_token
from src.web.job_manager import JobManager
from src.web.routes_direct import router as direct_router
from src.web.routes_jobs import router as jobs_router
from src.web.routes_media import router as media_router
from src.web.routes_tg import router as tg_router
from src.web.security import (
    get_auth_rate_limiter,
    get_ephemeral_token,
    init_security,
    mask_phone,
    verify_origin,
    verify_token,
)
from src.web.session_manager import SessionManager
from src.web.session_security import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    generate_session_id,
    get_session_id_from_request,
    set_session_cookie,
)

__all__ = ["SESSION_COOKIE_NAME", "clear_session_cookie", "create_app", "get_client_ip"]


def get_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first_ip = xff.split(",")[0].strip()
        if first_ip:
            return first_ip
    x_real = request.headers.get("x-real-ip")
    if x_real and x_real.strip():
        return x_real.strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def create_app(cfg: Config | None = None, out_dir: str | Path | None = None) -> FastAPI:
    if get_ephemeral_token() is None:
        init_security()

    if cfg is None:
        try:
            cfg = load_config()
        except (Exception, SystemExit):
            cfg = None

    session_manager = SessionManager(cfg=cfg, base_out_dir=out_dir or "out")

    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sweeper_task = None

        async def _sweeper_loop():
            while True:
                await asyncio.sleep(60.0)
                with suppress(Exception):
                    await session_manager.cleanup_idle_sessions()

        sweeper_task = asyncio.create_task(_sweeper_loop())
        yield
        if sweeper_task:
            sweeper_task.cancel()
            with suppress(Exception):
                await sweeper_task
        with suppress(Exception):
            await session_manager.close_all()
        jm = getattr(app.state, "job_manager", None)
        if jm:
            with suppress(Exception):
                await jm.cancel_job_async(timeout=10.0)
        if getattr(app.state, "tg_client", None):
            with suppress(Exception):
                if app.state.tg_client.is_connected():
                    await app.state.tg_client.disconnect()

    app = FastAPI(title="TeleVault Web Dashboard", lifespan=lifespan)
    default_hosts = ["127.0.0.1", "localhost", "[::1]", "testserver", "televault.madhudadi.in"]
    extra_hosts = [h.strip() for h in os.getenv("TELEVAULT_ALLOWED_HOSTS", "").split(",") if h.strip()]
    allowed_hosts = list(dict.fromkeys(default_hosts + extra_hosts))
    app.state.session_manager = session_manager
    app.state.job_manager = JobManager()
    app.state.out_dir = str(out_dir) if out_dir is not None else os.getenv("TELEVAULT_OUT_DIR", "out")
    app.state.config = cfg
    app.state.tg_client = None

    # Security & CSRF Middleware
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path

        # 1. Health check bypass
        if path.rstrip("/") == "/api/health":
            return await call_next(request)

        # 2. Origin check for state-changing requests
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            port = request.url.port or 8000
            if not verify_origin(origin, port):
                return JSONResponse(
                    status_code=403,
                    content={"error": "FORBIDDEN", "detail": "Cross-origin request rejected"},
                )

        # 3. Session cookie tracking
        session_id = get_session_id_from_request(request)
        new_session_id = None
        if not session_id:
            new_session_id = generate_session_id()
            request.state.session_id = new_session_id
        else:
            request.state.session_id = session_id

        # 4. Token & Visitor Auth Policy on /api/*
        if path.startswith("/api/"):
            client_ip = get_client_ip(request)
            limiter = get_auth_rate_limiter()

            raw_token = extract_master_token(request)

            if raw_token is not None:
                if limiter.is_rate_limited(client_ip):
                    retry_after = limiter.get_retry_after(client_ip)
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "TOO_MANY_REQUESTS",
                            "detail": "Too many failed authentication attempts. Please try again later.",
                        },
                        headers={"Retry-After": str(retry_after)},
                    )
                if not verify_token(raw_token):
                    limiter.record_failure(client_ip)
                    return JSONResponse(
                        status_code=401,
                        content={"error": "UNAUTHORIZED", "detail": "Invalid or missing token"},
                    )
                limiter.reset(client_ip)
                is_master_authenticated = True
            else:
                is_master_authenticated = False

            if not is_master_authenticated:
                # Public auth & health endpoints allowed for visitor login flow
                if path.startswith(("/api/auth/", "/api/health")):
                    pass
                elif path == "/api/status":
                    return JSONResponse(
                        status_code=401,
                        content={"error": "UNAUTHORIZED", "detail": "Invalid or missing token"},
                    )
                else:
                    # Endpoints requiring Telegram session (e.g. /api/dialogs, /api/download/*, /api/media*)
                    sid = get_session_id_from_request(request)
                    is_visitor_authorized = False
                    if sid and sid in session_manager._sessions:
                        tenant = session_manager._sessions[sid]
                        if getattr(tenant, "is_authorized", False):
                            is_visitor_authorized = True
                    if not is_visitor_authorized:
                        return JSONResponse(
                            status_code=401,
                            content={"error": "UNAUTHORIZED", "detail": "Telegram authentication required"},
                        )

        response = await call_next(request)

        # 5. Set session cookie on response if newly generated
        if new_session_id:
            is_secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            set_session_cookie(response, new_session_id, secure=is_secure)

        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )

    app.include_router(jobs_router)
    app.include_router(tg_router)
    app.include_router(media_router)
    app.include_router(direct_router)

    app.mount("/static", StaticFiles(directory=str(static_dir), html=True), name="static")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "app": "TeleVault"}

    @app.get("/")
    async def root():
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"status": "ok", "app": "TeleVault Web Dashboard"}

    @app.get("/api/status")
    async def status(request: Request):
        c = getattr(request.app.state, "config", None)
        session_path = (
            getattr(c, "session_path", Path("session/telegram.session")) if c else Path("session/telegram.session")
        )
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
