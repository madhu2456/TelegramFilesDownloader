"""FastAPI application factory with ephemeral security, origin validation, and static serving."""
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.cli import get_client
from src.config import Config, load_config, scrub_for_log
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

    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if cfg and not getattr(app.state, "tg_client", None):
            with suppress(Exception):
                app.state.tg_client = get_client(cfg)
        yield
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
    app.state.job_manager = JobManager()
    app.state.out_dir = str(out_dir) if out_dir is not None else os.getenv("TELEVAULT_OUT_DIR", "out")
    app.state.config = cfg
    app.state.tg_client = None

    # Security & CSRF Middleware
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path

        # Health check bypass
        if path.rstrip("/") == "/api/health":
            return await call_next(request)

        # Origin validation for state-changing requests
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            port = request.url.port or 8000
            if not verify_origin(origin, port):
                return JSONResponse(
                    status_code=403,
                    content={"error": "FORBIDDEN", "detail": "Cross-origin request rejected"},
                )

        # Token validation & rate limiting on /api/* endpoints
        if path.startswith("/api/"):
            client_ip = get_client_ip(request)
            limiter = get_auth_rate_limiter()
            if limiter.is_rate_limited(client_ip):
                retry_after = limiter.get_retry_after(client_ip)
                return JSONResponse(
                    status_code=429,
                    content={"error": "TOO_MANY_REQUESTS", "detail": "Too many failed authentication attempts. Please try again later."},
                    headers={"Retry-After": str(retry_after)},
                )

            auth_header = request.headers.get("authorization")
            bearer_token = auth_header.replace("Bearer ", "").strip() if auth_header else None
            token = (
                request.headers.get("x-auth-token")
                or request.query_params.get("token")
                or bearer_token
            )
            if not verify_token(token):
                limiter.record_failure(client_ip)
                return JSONResponse(
                    status_code=401,
                    content={"error": "UNAUTHORIZED", "detail": "Invalid or missing token"},
                )
            limiter.reset(client_ip)

        response = await call_next(request)
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
            getattr(c, "session_path", Path("session/telegram.session"))
            if c
            else Path("session/telegram.session")
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
