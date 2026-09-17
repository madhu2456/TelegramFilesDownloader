"""FastAPI application factory with ephemeral security, origin validation, and static serving."""

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
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
    os.umask(0o077)
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

        # 1. Health check & discovery endpoints bypass (avoid setting cookies on crawlers)
        if path == "/.well-known" or path.startswith("/.well-known/") or path.rstrip("/") in (
            "/api/health",
            "/robots.txt",
            "/sitemap.xml",
            "/llms.txt",
            "/llms-full.txt",
            "/ai-profile.json",
        ):
            response = await call_next(request)
            if hasattr(response.headers, "pop"):
                response.headers.pop("set-cookie", None)
            elif "set-cookie" in response.headers:
                del response.headers["set-cookie"]
            return response

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

        if path in ("/", "/app", "/app/") or path.startswith("/static/"):
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
    async def root() -> Response:
        landing_file = static_dir / "landing.html"
        if landing_file.is_file():
            return FileResponse(landing_file)
        index_file = static_dir / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return JSONResponse({"status": "ok", "app": "TeleVault Web Dashboard"})

    @app.get("/app")
    @app.get("/app/")
    async def app_dashboard() -> Response:
        index_file = static_dir / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return JSONResponse({"status": "ok", "app": "TeleVault Web Dashboard"})

    @app.get("/robots.txt")
    async def robots_txt() -> Response:
        rf = static_dir / "robots.txt"
        if rf.is_file():
            return FileResponse(rf, media_type="text/plain; charset=utf-8")
        fallback_robots = (
            "User-agent: GPTBot\n"
            "User-agent: ClaudeBot\n"
            "User-agent: CCBot\n"
            "User-agent: Bytespider\n"
            "User-agent: anthropic-ai\n"
            "Disallow: /\n\n"
            "User-agent: PerplexityBot\n"
            "User-agent: OAI-SearchBot\n"
            "User-agent: Bingbot\n"
            "User-agent: Google-Extended\n"
            "User-agent: Applebot-Extended\n"
            "Allow: /\n"
            "Allow: /app\n"
            "Allow: /sitemap.xml\n"
            "Allow: /llms.txt\n"
            "Allow: /llms-full.txt\n"
            "Allow: /ai-profile.json\n"
            "Disallow: /api/\n"
            "Disallow: /ws/\n\n"
            "User-agent: *\n"
            "Allow: /\n"
            "Allow: /app\n"
            "Allow: /sitemap.xml\n"
            "Allow: /llms.txt\n"
            "Allow: /llms-full.txt\n"
            "Allow: /ai-profile.json\n"
            "Disallow: /api/\n"
            "Disallow: /ws/\n\n"
            "Sitemap: https://televault.madhudadi.in/sitemap.xml\n"
        )
        return PlainTextResponse(fallback_robots, media_type="text/plain; charset=utf-8")

    @app.get("/sitemap.xml")
    async def sitemap_xml() -> Response:
        sf = static_dir / "sitemap.xml"
        if sf.is_file():
            return FileResponse(sf, media_type="application/xml")
        fallback_sitemap = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "  <url><loc>https://televault.madhudadi.in/</loc></url>\n"
            "  <url><loc>https://televault.madhudadi.in/app</loc></url>\n"
            "</urlset>\n"
        )
        return PlainTextResponse(fallback_sitemap, media_type="application/xml")

    @app.get("/llms.txt")
    async def llms_txt() -> Response:
        lf = static_dir / "llms.txt"
        if lf.is_file():
            return FileResponse(lf, media_type="text/plain; charset=utf-8")
        return PlainTextResponse("# TeleVault\nDirect browser streaming for Telegram.\n")

    @app.get("/llms-full.txt")
    async def llms_full_txt() -> Response:
        lf = static_dir / "llms-full.txt"
        if lf.is_file():
            return FileResponse(lf, media_type="text/plain; charset=utf-8")
        return PlainTextResponse("# TeleVault Full Documentation\n")

    @app.get("/ai-profile.json")
    async def ai_profile_json() -> Response:
        apf = static_dir / "ai-profile.json"
        if apf.is_file():
            return FileResponse(apf, media_type="application/json; charset=utf-8")
        return JSONResponse(
            status_code=200,
            content={
                "name": "TeleVault",
                "url": "https://televault.madhudadi.in/",
                "description": "High-performance direct browser streaming and MTProto downloader for Telegram media up to 4GB with zero server disk footprint.",
                "publisher": {
                    "@type": "Organization",
                    "@id": "https://televault.madhudadi.in/#organization",
                    "name": "TeleVault",
                    "url": "https://televault.madhudadi.in/",
                    "parentOrganization": {
                        "@type": "Organization",
                        "@id": "https://madhudadi.in/#organization",
                        "name": "Madhu Dadi Ecosystem",
                        "url": "https://madhudadi.in/",
                    },
                },
                "author": {
                    "@type": "Person",
                    "@id": "https://madhudadi.in/#person",
                    "name": "Madhu Dadi",
                    "url": "https://madhudadi.in/profile/",
                    "sameAs": [
                        "https://www.wikidata.org/wiki/Q139807441",
                        "https://github.com/madhu2456",
                        "https://www.linkedin.com/in/madhu-dadi-54684531",
                        "https://x.com/madhu245",
                        "https://medium.com/@madhu.kumar245",
                        "https://dev.to/madhudadi",
                        "https://www.youtube.com/@madhukumar245",
                        "https://maps.google.com/?cid=CXaUijPkQhVkEBM",
                    ],
                },
            },
            media_type="application/json",
        )

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
