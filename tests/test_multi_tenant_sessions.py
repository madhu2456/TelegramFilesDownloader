"""Multi-tenant visitor session isolation, path jailing, and pool lifecycle tests."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

import src.web.security as sec
from src.config import Config
from src.web.app import create_app
from src.web.client_helpers import (
    get_session_client,
    get_session_job_manager,
    get_session_lock,
    get_session_out_dir,
    get_tenant_session,
)
from src.web.job_manager import JobManager
from src.web.security import (
    get_auth_rate_limiter,
    get_ephemeral_token,
    get_phone_auth_limiter,
    get_qr_auth_limiter,
    set_ephemeral_token,
)
from src.web.session_manager import (
    SessionCapacityExceededError,
    SessionManager,
    TenantSession,
)
from src.web.session_security import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    generate_session_id,
    get_canonical_out_dir,
    get_canonical_session_path,
    get_session_id_from_request,
    set_session_cookie,
    validate_session_id,
)


@pytest.fixture(autouse=True)
def isolate_session_and_token_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate token file, session paths, and auth limiters during testing."""
    isolated_token_file = tmp_path / "tele_vault_test" / ".token"
    monkeypatch.setenv("TELEVAULT_TOKEN_FILE", str(isolated_token_file))
    monkeypatch.setenv("TELEVAULT_ALLOWED_HOSTS", "testserver")
    monkeypatch.delenv("TELEVAULT_TOKEN", raising=False)

    sec._ACTIVE_TOKEN = None
    sec._ACTIVE_TOKEN_HASH = None
    sec._TOKEN_SOURCE = "unset"

    get_auth_rate_limiter().reset()
    get_phone_auth_limiter()._global_attempts.clear()
    get_phone_auth_limiter()._ip_attempts.clear()
    get_qr_auth_limiter()._global_attempts.clear()
    get_qr_auth_limiter()._ip_attempts.clear()

    # Route canonical session path to tmp_path / "sessions_base"
    sessions_base = tmp_path / "sessions_base"
    sessions_base.mkdir(parents=True, exist_ok=True)
    orig_canonical_sess = get_canonical_session_path

    def _isolated_canonical_sess(sid: str, base_dir: Path | str | None = None) -> Path:
        target_base = Path(base_dir) if base_dir is not None else sessions_base
        return orig_canonical_sess(sid, base_dir=target_base)

    monkeypatch.setattr("src.web.session_security.get_canonical_session_path", _isolated_canonical_sess)
    globals()["get_canonical_session_path"] = _isolated_canonical_sess

    Path("session/test_visitor_logout_sess_123456.session").unlink(missing_ok=True)
    yield
    globals()["get_canonical_session_path"] = orig_canonical_sess
    Path("session/test_visitor_logout_sess_123456.session").unlink(missing_ok=True)

    sec._ACTIVE_TOKEN = None
    sec._ACTIVE_TOKEN_HASH = None
    sec._TOKEN_SOURCE = "unset"
    get_auth_rate_limiter().reset()
    get_phone_auth_limiter()._global_attempts.clear()
    get_phone_auth_limiter()._ip_attempts.clear()
    get_qr_auth_limiter()._global_attempts.clear()
    get_qr_auth_limiter()._ip_attempts.clear()


def _make_config(tmp_path: Path) -> Config:
    return Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )


# ============================================================================
# 1. Session Security & Path Jailing
# ============================================================================


def test_validate_session_id_entropy_and_boundaries():
    """Test validate_session_id with valid high-entropy strings, boundary lengths, invalid chars, and non-strings."""
    # 1. Generated high-entropy session IDs
    generated = generate_session_id()
    assert isinstance(generated, str)
    assert len(generated) >= 32
    assert validate_session_id(generated) is True

    # 2. Boundary lengths (16 to 64 chars)
    min_len_valid = "a" * 16
    max_len_valid = "b" * 64
    mid_len_valid = "c" * 32
    assert validate_session_id(min_len_valid) is True
    assert validate_session_id(max_len_valid) is True
    assert validate_session_id(mid_len_valid) is True
    assert validate_session_id("abcABC123_-" * 2) is True  # Allowed characters

    # 3. Short strings (< 16 chars)
    assert validate_session_id("") is False
    assert validate_session_id("a") is False
    assert validate_session_id("a" * 15) is False
    assert validate_session_id("1234567890abcde") is False

    # 4. Overly long strings (> 64 chars)
    assert validate_session_id("a" * 65) is False
    assert validate_session_id("b" * 100) is False

    # 5. Invalid characters & path traversal attempts
    assert validate_session_id("../../etc/passwd") is False
    assert validate_session_id("../session_other") is False
    assert validate_session_id("sess.ion.id.12345") is False
    assert validate_session_id("session/sub/1234") is False
    assert validate_session_id("session\\sub\\1234") is False
    assert validate_session_id("session id spaces") is False
    assert validate_session_id("sess;rm -rf /") is False
    assert validate_session_id("sess!@#$%^&*()_+") is False
    assert validate_session_id("<script>alert(1)</script>") is False

    # 6. None and non-string types
    assert validate_session_id(None) is False
    assert validate_session_id(1234567890123456) is False  # type: ignore[arg-type]
    assert validate_session_id(["session_id_123456"]) is False  # type: ignore[arg-type]
    assert validate_session_id({"session": "id"}) is False  # type: ignore[arg-type]


def test_get_canonical_session_path_resolution_and_traversal(tmp_path: Path):
    """Test get_canonical_session_path path resolution within session directory and traversal blocking."""
    base_dir = tmp_path / "custom_sessions"
    valid_sid = "valid_session_123456789_abcdef"

    # 1. Valid resolution
    p = get_canonical_session_path(valid_sid, base_dir=base_dir)
    assert p == (base_dir.resolve() / f"{valid_sid}.session")
    assert p.is_relative_to(base_dir.resolve())
    assert base_dir.exists()

    # 2. Path traversal attempts raise ValueError
    traversal_inputs = [
        "../../etc/passwd",
        "../traversal_sid_123456",
        "nested/path_1234567890",
        "evil\\slash_1234567890",
        "/absolute/root_123456",
    ]
    for bad_sid in traversal_inputs:
        with pytest.raises(ValueError, match=r"(Invalid session ID|traversal)"):
            get_canonical_session_path(bad_sid, base_dir=base_dir)

    # 3. Invalid format or short IDs raise ValueError
    for bad_sid in ["short", "", "a" * 15, "a" * 65]:
        with pytest.raises(ValueError, match="Invalid session ID"):
            get_canonical_session_path(bad_sid, base_dir=base_dir)

    # 4. None raises ValueError
    with pytest.raises(ValueError, match="Invalid session ID"):
        get_canonical_session_path(None, base_dir=base_dir)  # type: ignore[arg-type]

    # 5. Direct path traversal check if is_relative_to fails
    with (
        patch("src.web.session_security.validate_session_id", return_value=True),
        patch("pathlib.Path.is_relative_to", return_value=False),
        pytest.raises(ValueError, match="Session path traversal detected"),
    ):
        get_canonical_session_path("valid_sid_bypass_12345", base_dir=base_dir)


def test_get_canonical_out_dir_creation_and_traversal(tmp_path: Path):
    """Test get_canonical_out_dir ensures directory is created under out/sessions/<session_id> and blocks traversal."""
    base_out = tmp_path / "out"
    valid_sid = "tenant_out_dir_123456789"

    # 1. Valid directory creation
    d = get_canonical_out_dir(valid_sid, base_out_dir=base_out)
    expected = (base_out / "sessions" / valid_sid).resolve()
    assert d == expected
    assert d.exists()
    assert d.is_dir()
    assert d.is_relative_to((base_out / "sessions").resolve())

    # 2. Path traversal attempts raise ValueError
    for bad_sid in ["../../etc/passwd", "../other_out", "sub/dir/123456789"]:
        with pytest.raises(ValueError, match=r"(Invalid session ID|traversal)"):
            get_canonical_out_dir(bad_sid, base_out_dir=base_out)

    # 3. Invalid IDs raise ValueError
    for bad_sid in ["short", "", "invalid!chars!12345"]:
        with pytest.raises(ValueError, match="Invalid session ID"):
            get_canonical_out_dir(bad_sid, base_out_dir=base_out)

    # 4. Out dir traversal check if is_relative_to fails
    with (
        patch("src.web.session_security.validate_session_id", return_value=True),
        patch("pathlib.Path.is_relative_to", return_value=False),
        pytest.raises(ValueError, match="Output directory traversal detected"),
    ):
        get_canonical_out_dir("valid_sid_bypass_12345", base_out_dir=base_out)


def test_session_cookie_management_and_request_extraction():
    """Test set_session_cookie, clear_session_cookie, and get_session_id_from_request."""
    valid_sid = "cookie_test_session_12345678"
    response = Response()

    # 1. Set cookie
    set_session_cookie(response, valid_sid, secure=True)
    raw_cookie = response.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE_NAME}={valid_sid}" in raw_cookie
    assert "HttpOnly" in raw_cookie
    assert "samesite=lax" in raw_cookie.lower()
    assert "Secure" in raw_cookie
    assert "Max-Age=2592000" in raw_cookie

    # 2. Clear cookie
    clear_response = Response()
    clear_session_cookie(clear_response)
    clear_cookie_hdr = clear_response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in clear_cookie_hdr
    assert "max-age=0" in clear_cookie_hdr.lower() or "expires=" in clear_cookie_hdr.lower()

    # 3. Extract session ID from request
    req_valid = Request({"type": "http", "headers": [(b"cookie", f"{SESSION_COOKIE_NAME}={valid_sid}".encode())]})
    assert get_session_id_from_request(req_valid) == valid_sid

    req_invalid = Request({"type": "http", "headers": [(b"cookie", f"{SESSION_COOKIE_NAME}=bad../id".encode())]})
    assert get_session_id_from_request(req_invalid) is None

    req_empty = Request({"type": "http", "headers": []})
    assert get_session_id_from_request(req_empty) is None


# ============================================================================
# 2. Public Visitor Cookie & Endpoint Flow
# ============================================================================


def test_public_visitor_cookie_generation_on_root_and_auth_me(tmp_path: Path):
    """Accessing / or /api/auth/me sets televault_session cookie (httponly=True, samesite=lax)."""
    cfg = _make_config(tmp_path)
    app = create_app(cfg, out_dir=tmp_path / "out")
    client = TestClient(app)

    # 1. Accessing root /
    res_root = client.get("/")
    assert res_root.status_code == 200
    set_cookie_root = res_root.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie_root
    assert "httponly" in set_cookie_root.lower()
    assert "samesite=lax" in set_cookie_root.lower()

    cookie_val = res_root.cookies.get(SESSION_COOKIE_NAME)
    assert cookie_val is not None
    assert validate_session_id(cookie_val) is True

    # 2. Accessing /api/auth/me with fresh client
    fresh_client = TestClient(app)
    res_auth = fresh_client.get("/api/auth/me")
    assert res_auth.status_code == 200
    set_cookie_auth = res_auth.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie_auth
    assert "httponly" in set_cookie_auth.lower()
    assert "samesite=lax" in set_cookie_auth.lower()

    # 3. Subsequent request with existing cookie does not re-issue cookie
    existing_sid = "existing_visitor_session_123456"
    subsequent_client = TestClient(app)
    subsequent_client.cookies[SESSION_COOKIE_NAME] = existing_sid
    res_subsequent = subsequent_client.get("/api/auth/me")
    assert res_subsequent.status_code == 200
    # Response should NOT issue a new set-cookie since cookie was already present
    assert "set-cookie" not in res_subsequent.headers or SESSION_COOKIE_NAME not in res_subsequent.headers.get(
        "set-cookie", ""
    )


def test_unauthenticated_auth_me_returns_authorized_false(tmp_path: Path):
    """Unauthenticated /api/auth/me returns HTTP 200 with {'authorized': False}."""
    cfg = _make_config(tmp_path)
    app = create_app(cfg, out_dir=tmp_path / "out")
    client = TestClient(app)

    res = client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.json() == {"authorized": False}


def test_unauthenticated_protected_endpoints_return_401(tmp_path: Path):
    """Unauthenticated requests to protected endpoints return HTTP 401 with UNAUTHORIZED detail."""
    cfg = _make_config(tmp_path)
    app = create_app(cfg, out_dir=tmp_path / "out")
    client = TestClient(app)

    # 1. GET /api/dialogs
    res_dialogs = client.get("/api/dialogs")
    assert res_dialogs.status_code == 401
    assert res_dialogs.json() == {
        "error": "UNAUTHORIZED",
        "detail": "Telegram authentication required",
    }

    # 2. POST /api/download/start (with valid Origin)
    res_dl = client.post(
        "/api/download/start",
        json={"target": "test_target", "action": "download"},
        headers={"Origin": "http://testserver"},
    )
    assert res_dl.status_code == 401
    assert res_dl.json() == {
        "error": "UNAUTHORIZED",
        "detail": "Telegram authentication required",
    }

    # 3. POST /api/resolve (with valid Origin)
    res_resolve = client.post(
        "/api/resolve",
        json={"target": "test_channel", "join": False},
        headers={"Origin": "http://testserver"},
    )
    assert res_resolve.status_code == 401
    assert res_resolve.json() == {
        "error": "UNAUTHORIZED",
        "detail": "Telegram authentication required",
    }

    # 4. GET /api/status (requires master token)
    res_status = client.get("/api/status")
    assert res_status.status_code == 401
    assert res_status.json() == {
        "error": "UNAUTHORIZED",
        "detail": "Invalid or missing token",
    }


def test_visitor_auth_logout_flow(tmp_path: Path):
    """POST /api/auth/logout removes/disconnects tenant, unlinks session file, and clears cookie."""
    cfg = _make_config(tmp_path)
    app = create_app(cfg, out_dir=tmp_path / "out")
    sm: SessionManager = app.state.session_manager

    session_id = "test_visitor_logout_sess_123456"
    mock_client = AsyncMock()
    mock_client.log_out = AsyncMock()
    mock_client.disconnect = AsyncMock()

    # Pre-create tenant session in SessionManager
    tenant = TenantSession(
        session_id=session_id,
        client=mock_client,
        job_manager=JobManager(),
        out_dir=tmp_path / "out" / "sessions" / session_id,
        lock=asyncio.Lock(),
        last_accessed=time.time(),
    )
    sm._sessions[session_id] = tenant

    # Pre-create session file on disk
    sess_path = get_canonical_session_path(session_id)
    sess_path.parent.mkdir(parents=True, exist_ok=True)
    sess_path.write_bytes(b"dummy_session_data")
    assert sess_path.exists()

    client = TestClient(app)
    client.cookies[SESSION_COOKIE_NAME] = session_id
    res = client.post(
        "/api/auth/logout",
        headers={"Origin": "http://testserver"},
    )

    assert res.status_code == 200
    assert res.json() in [{"status": "ok"}, {"status": "logged_out"}]

    # Tenant removed from session manager pool
    assert session_id not in sm._sessions

    # Client log_out and disconnect invoked
    mock_client.log_out.assert_awaited_once()
    mock_client.disconnect.assert_awaited_once()

    # Session file unlinked
    assert not sess_path.exists()

    # Session cookie cleared in response headers
    set_cookie_hdr = res.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie_hdr
    assert "max-age=0" in set_cookie_hdr.lower() or "expires=" in set_cookie_hdr.lower()


# ============================================================================
# 3. Master-Token Precedence
# ============================================================================


def test_master_token_precedence_bypasses_visitor_gating(tmp_path: Path):
    """When a valid master token is supplied, requests bypass visitor gating and access admin capabilities."""
    cfg = _make_config(tmp_path)
    app = create_app(cfg, out_dir=tmp_path / "out")
    token = get_ephemeral_token()
    assert token is not None

    client = TestClient(app)

    # 1. GET /api/status with X-Auth-Token header
    res_status_hdr = client.get("/api/status", headers={"X-Auth-Token": token})
    assert res_status_hdr.status_code == 200
    assert res_status_hdr.json()["status"] == "ok"

    # 2. GET /api/status with Authorization: Bearer <token>
    res_status_bearer = client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
    assert res_status_bearer.status_code == 200
    assert res_status_bearer.json()["status"] == "ok"

    # 3. GET /api/status with query parameter ?token=<token>
    res_status_query = client.get(f"/api/status?token={token}")
    assert res_status_query.status_code == 200
    assert res_status_query.json()["status"] == "ok"

    # 4. Master token bypasses visitor gating on /api/dialogs
    mock_master_tg = AsyncMock()
    mock_master_tg.is_connected = MagicMock(return_value=True)
    app.state.tg_client = mock_master_tg

    with patch("src.web.routes_tg.fetch_dialog_rows", new=AsyncMock(return_value=[{"id": 100, "name": "General"}])):
        res_dialogs = client.get("/api/dialogs", headers={"X-Auth-Token": token})
        assert res_dialogs.status_code == 200
        data = res_dialogs.json()
        assert "dialogs" in data
        assert len(data["dialogs"]) == 1
        assert data["dialogs"][0]["id"] == 100

    # 5. Invalid master token returns 401 and does not fall back to visitor session
    res_bad_token = client.get("/api/status", headers={"X-Auth-Token": "invalid_master_token_12345"})
    assert res_bad_token.status_code == 401
    assert res_bad_token.json() == {
        "error": "UNAUTHORIZED",
        "detail": "Invalid or missing token",
    }


@pytest.mark.anyio
async def test_client_helpers_master_token_precedence(tmp_path: Path):
    """Test get_session_client, get_session_job_manager, and get_session_out_dir honor master token precedence."""
    master_client = AsyncMock()
    master_job_manager = JobManager()
    master_out = tmp_path / "master_out"

    tenant_client = AsyncMock()
    tenant_job_manager = JobManager()
    tenant_out = tmp_path / "tenant_out"

    master_token = "valid_master_token_1234567890123456"
    set_ephemeral_token(master_token)

    session_id = "tenant_session_id_123456789"
    sm = SessionManager(base_out_dir=tmp_path)
    tenant = TenantSession(
        session_id=session_id,
        client=tenant_client,
        job_manager=tenant_job_manager,
        out_dir=tenant_out,
        lock=asyncio.Lock(),
        last_accessed=time.time(),
    )
    sm._sessions[session_id] = tenant

    # Simulated app state
    class DummyState:
        tg_client = master_client
        job_manager = master_job_manager
        out_dir = str(master_out)
        session_manager = sm

    dummy_app = MagicMock()
    dummy_app.state = DummyState()

    # Case A: Master token present -> returns master objects even when visitor cookie is passed
    req_master = Request(
        {
            "type": "http",
            "app": dummy_app,
            "headers": [
                (b"x-auth-token", master_token.encode()),
                (b"cookie", f"{SESSION_COOKIE_NAME}={session_id}".encode()),
            ],
            "query_string": b"",
        }
    )
    req_master.scope["app"] = dummy_app

    resolved_client = await get_session_client(req_master)
    assert resolved_client is master_client

    resolved_jm = get_session_job_manager(req_master)
    assert resolved_jm is master_job_manager

    resolved_out = get_session_out_dir(req_master)
    assert resolved_out == master_out

    # Case B: No master token -> returns tenant objects
    req_tenant = Request(
        {
            "type": "http",
            "app": dummy_app,
            "headers": [
                (b"cookie", f"{SESSION_COOKIE_NAME}={session_id}".encode()),
            ],
            "query_string": b"",
        }
    )
    req_tenant.scope["app"] = dummy_app

    resolved_tenant_cli = await get_session_client(req_tenant)
    assert resolved_tenant_cli is tenant_client

    resolved_tenant_jm = get_session_job_manager(req_tenant)
    assert resolved_tenant_jm is tenant_job_manager

    resolved_tenant_out = get_session_out_dir(req_tenant)
    assert resolved_tenant_out == tenant_out

    resolved_lock = get_session_lock(req_tenant)
    assert resolved_lock is tenant.lock

    resolved_tenant = get_tenant_session(req_tenant)
    assert resolved_tenant is tenant


# ============================================================================
# 4. Bounded Pool & Capacity Exceeded Handling
# ============================================================================


@pytest.mark.anyio
async def test_session_manager_lru_eviction(tmp_path: Path):
    """Test SessionManager LRU eviction drops the least-recently used non-running tenant when full."""
    sm = SessionManager(base_out_dir=tmp_path)
    sm.MAX_ACTIVE_CLIENTS = 2

    # Create dummy session files so allow_jit=False or True succeeds
    sids = ["session_one_1234567890", "session_two_1234567890", "session_three_1234567890"]
    for sid in sids:
        p = get_canonical_session_path(sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()

    # 1. Fill pool to capacity (2 clients)
    t1 = await sm.get_or_create_tenant(sids[0], allow_jit=True)
    t2 = await sm.get_or_create_tenant(sids[1], allow_jit=True)
    assert t1 is not None and t2 is not None

    mock_cli1 = AsyncMock()
    mock_cli2 = AsyncMock()
    t1.client = mock_cli1
    t2.client = mock_cli2

    assert len(sm._sessions) == 2
    assert list(sm._sessions.keys()) == [sids[0], sids[1]]

    # 2. Access t1 to make it most recently used (t2 becomes LRU candidate)
    await sm.get_or_create_tenant(sids[0])
    assert list(sm._sessions.keys()) == [sids[1], sids[0]]

    # 3. Add t3 -> triggers LRU eviction of t2
    t3 = await sm.get_or_create_tenant(sids[2], allow_jit=True)
    assert t3 is not None
    assert len(sm._sessions) == 2
    assert sids[1] not in sm._sessions
    assert sids[0] in sm._sessions
    assert sids[2] in sm._sessions

    # Verify t2's client was disconnected
    mock_cli2.disconnect.assert_awaited_once()
    mock_cli1.disconnect.assert_not_awaited()


@pytest.mark.anyio
async def test_session_manager_capacity_exceeded_error(tmp_path: Path):
    """SessionCapacityExceededError raised when all pool slots are occupied by actively downloading tenants."""
    sm = SessionManager(base_out_dir=tmp_path)
    sm.MAX_ACTIVE_CLIENTS = 2

    sids = ["running_sess_one_123456", "running_sess_two_123456", "pending_sess_three_123456"]
    for sid in sids:
        p = get_canonical_session_path(sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()

    t1 = await sm.get_or_create_tenant(sids[0], allow_jit=True)
    t2 = await sm.get_or_create_tenant(sids[1], allow_jit=True)
    assert t1 is not None and t2 is not None

    # Mark both job managers as actively running downloads
    t1.job_manager.is_running = MagicMock(return_value=True)
    t2.job_manager.is_running = MagicMock(return_value=True)

    # Attempting to allocate a 3rd tenant must raise SessionCapacityExceededError
    with pytest.raises(SessionCapacityExceededError, match="Server download capacity reached") as exc_info:
        await sm.get_or_create_tenant(sids[2], allow_jit=True)
    assert exc_info.value.status_code == 503
    assert exc_info.value.headers.get("Retry-After") == "60"


@pytest.mark.anyio
async def test_session_manager_cleanup_idle_sessions(tmp_path: Path):
    """cleanup_idle_sessions() disconnects idle tenants exceeding TTL while preserving active downloads."""
    sm = SessionManager(base_out_dir=tmp_path)
    sm.IDLE_TTL_SECONDS = 300.0  # 5 minutes TTL

    now = time.time()
    sid_idle = "idle_tenant_session_1234567"
    sid_busy = "busy_tenant_session_1234567"
    sid_fresh = "fresh_tenant_session_123456"

    cli_idle = AsyncMock()
    cli_busy = AsyncMock()
    cli_fresh = AsyncMock()

    jm_idle = JobManager()
    jm_busy = JobManager()
    jm_busy.is_running = MagicMock(return_value=True)  # Busy downloading
    jm_fresh = JobManager()

    # 1. Idle tenant: last accessed 600s ago, not downloading -> should be swept
    t_idle = TenantSession(
        session_id=sid_idle,
        client=cli_idle,
        job_manager=jm_idle,
        out_dir=tmp_path / sid_idle,
        lock=asyncio.Lock(),
        last_accessed=now - 600.0,
    )

    # 2. Busy tenant: last accessed 600s ago, but actively downloading -> should NOT be swept
    t_busy = TenantSession(
        session_id=sid_busy,
        client=cli_busy,
        job_manager=jm_busy,
        out_dir=tmp_path / sid_busy,
        lock=asyncio.Lock(),
        last_accessed=now - 600.0,
    )

    # 3. Fresh tenant: last accessed 50s ago, not downloading -> should NOT be swept
    t_fresh = TenantSession(
        session_id=sid_fresh,
        client=cli_fresh,
        job_manager=jm_fresh,
        out_dir=tmp_path / sid_fresh,
        lock=asyncio.Lock(),
        last_accessed=now - 50.0,
    )

    sm._sessions[sid_idle] = t_idle
    sm._sessions[sid_busy] = t_busy
    sm._sessions[sid_fresh] = t_fresh

    swept_count = await sm.cleanup_idle_sessions()
    assert swept_count == 1

    # Verified idle tenant was swept and disconnected
    assert sid_idle not in sm._sessions
    cli_idle.disconnect.assert_awaited_once()

    # Verified busy and fresh tenants remain connected
    assert sid_busy in sm._sessions
    assert sid_fresh in sm._sessions
    cli_busy.disconnect.assert_not_awaited()
    cli_fresh.disconnect.assert_not_awaited()


@pytest.mark.anyio
async def test_session_manager_close_all(tmp_path: Path):
    """close_all() gracefully disconnects all active clients in the pool."""
    sm = SessionManager(base_out_dir=tmp_path)
    cli1 = AsyncMock()
    cli2 = AsyncMock()

    sm._sessions["sess1_123456789012"] = TenantSession(
        session_id="sess1_123456789012",
        client=cli1,
        job_manager=JobManager(),
        out_dir=tmp_path / "1",
        lock=asyncio.Lock(),
        last_accessed=time.time(),
    )
    sm._sessions["sess2_123456789012"] = TenantSession(
        session_id="sess2_123456789012",
        client=cli2,
        job_manager=JobManager(),
        out_dir=tmp_path / "2",
        lock=asyncio.Lock(),
        last_accessed=time.time(),
    )

    await sm.close_all()
    assert len(sm._sessions) == 0
    cli1.disconnect.assert_awaited_once()
    cli2.disconnect.assert_awaited_once()


@pytest.mark.anyio
async def test_session_manager_zero_jit_jailing(tmp_path: Path):
    """get_or_create_tenant returns None when session file does not exist and allow_jit=False."""
    sm = SessionManager(base_out_dir=tmp_path)
    non_existent_sid = "non_existent_sess_12345678"

    # allow_jit=False on non-existent session file returns None
    tenant = await sm.get_or_create_tenant(non_existent_sid, allow_jit=False)
    assert tenant is None
    assert non_existent_sid not in sm._sessions

    # allow_jit=True succeeds even without existing session file
    tenant_jit = await sm.get_or_create_tenant(non_existent_sid, allow_jit=True)
    assert tenant_jit is not None
    assert tenant_jit.session_id == non_existent_sid
    assert non_existent_sid in sm._sessions


@pytest.mark.anyio
async def test_master_token_bearer_precedence_over_visitor_cookie(tmp_path: Path):
    """Authorization: Bearer <token> retains strict precedence over visitor cookies in client_helpers."""
    from starlette.requests import Request

    from src.web.client_helpers import get_session_client, get_session_job_manager, get_session_out_dir
    from src.web.security import init_security

    master_token = init_security()
    cfg = _make_config(tmp_path)
    app = create_app(cfg, out_dir=tmp_path / "out")
    mock_tg = MagicMock()
    app.state.tg_client = mock_tg

    # Construct request with visitor cookie AND Authorization: Bearer <master_token>
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/status",
        "headers": [
            (b"host", b"testserver"),
            (b"cookie", b"televault_session=visitor_cookie_sess_12345678"),
            (b"authorization", f"Bearer {master_token}".encode("ascii")),
        ],
        "query_string": b"",
        "app": app,
    }
    req = Request(scope)

    # Must resolve master client, master job manager, and root out_dir
    cli = await get_session_client(req)
    jm = get_session_job_manager(req)
    out = get_session_out_dir(req)

    assert cli is app.state.tg_client
    assert jm is app.state.job_manager
    assert out == Path(app.state.out_dir)


def test_websocket_live_rehydration_and_unauthorized_rejection(tmp_path: Path):
    """Verify WebSocket live telemetry rejects unauthorized connections with 1008 immediately and re-hydrates valid tenants."""
    from fastapi import WebSocketDisconnect

    from src.web.security import get_ephemeral_token

    cfg = _make_config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)

    # 1. Reject empty token and absent session cookie
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect("/ws/live?token="):
        pass
    assert exc_info.value.code == 1008

    # 2. Reject unknown session cookie without JIT creation
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/ws/live", cookies={SESSION_COOKIE_NAME: "sess_nonexistent_0123456789abcdef"}),
    ):
        pass
    assert exc_info.value.code == 1008

    # 3. Accept valid master token
    token = get_ephemeral_token()
    with client.websocket_connect(f"/ws/live?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "INIT_STATE"

