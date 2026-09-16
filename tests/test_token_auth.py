"""Comprehensive security, isolation, and boundary edge case tests for token auth."""
import hashlib
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.web.security as sec
from src.web.app import create_app
from src.web.security import (
    AuthFailureTracker,
    get_ephemeral_token,
    get_token_source,
    init_security,
    set_ephemeral_token,
    verify_token,
)


@pytest.fixture(autouse=True)
def isolate_token_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure repo root data/.token is NEVER touched during testing."""
    isolated_token_file = tmp_path / "tele_vault_test" / ".token"
    monkeypatch.setenv("TELEVAULT_TOKEN_FILE", str(isolated_token_file))
    monkeypatch.delenv("TELEVAULT_TOKEN", raising=False)
    sec._ACTIVE_TOKEN = None
    sec._ACTIVE_TOKEN_HASH = None
    sec._TOKEN_SOURCE = "unset"
    from src.web.security import get_auth_rate_limiter

    get_auth_rate_limiter().reset()
    yield isolated_token_file
    sec._ACTIVE_TOKEN = None
    sec._ACTIVE_TOKEN_HASH = None
    sec._TOKEN_SOURCE = "unset"
    get_auth_rate_limiter().reset()


def test_sha256_constant_time_verification_and_caching():
    """Verify match, whitespace stripping, length-difference failure without crash, and cached digest verification."""
    raw_token = "   sec-auth-token-xyz-1234567890abcdef   "
    expected_token = raw_token.strip()
    set_ephemeral_token(raw_token)

    assert get_ephemeral_token() == expected_token

    # Verify match and whitespace stripping
    assert verify_token(expected_token) is True
    assert verify_token(raw_token) is True
    assert verify_token(f"\n\t{expected_token}  \r\n") is True

    # Length-difference failure without crash
    assert verify_token("short") is False
    assert verify_token(expected_token + "-extra-long-suffix-that-changes-length") is False
    assert verify_token("a" * 10000) is False
    assert verify_token("") is False
    assert verify_token(None) is False
    assert verify_token("wrong-token-same-len-1234567890abcdef") is False

    # Cached digest verification
    expected_digest = hashlib.sha256(expected_token.encode("utf-8")).digest()
    assert expected_digest == sec._ACTIVE_TOKEN_HASH

    # Verify behavior when cached hash is absent
    original_hash = sec._ACTIVE_TOKEN_HASH
    try:
        sec._ACTIVE_TOKEN_HASH = None
        assert verify_token(expected_token) is False
    finally:
        sec._ACTIVE_TOKEN_HASH = original_hash


def test_token_persistence_atomic_replace_and_permissions(isolate_token_environment: Path):
    """Verify atomic file creation, mode 0o600, and get_token_source() == 'generated_persisted'."""
    assert isolate_token_environment.exists() is False

    token = init_security()

    assert isinstance(token, str)
    assert len(token) >= 32
    assert isolate_token_environment.exists() is True

    # Mode 0o600 verification
    file_mode = isolate_token_environment.stat().st_mode & 0o777
    assert file_mode == 0o600

    # Source check
    assert get_token_source() == "generated_persisted"

    # Persisted file content match
    content = isolate_token_environment.read_text(encoding="utf-8").strip()
    assert content == token
    assert verify_token(token) is True

    # Ensure no leftover temporary files in parent directory
    tmp_files = list(isolate_token_environment.parent.glob(".token.tmp.*"))
    assert len(tmp_files) == 0


def test_token_persistence_reloads_existing_file(isolate_token_environment: Path):
    """Pre-write a known token to isolate_token_environment, assert it reloads without regenerating."""
    known_token = "pre-existing-fixed-secret-token-abcdef123456"
    isolate_token_environment.parent.mkdir(parents=True, exist_ok=True)
    isolate_token_environment.write_text(f"{known_token}\n", encoding="utf-8")
    isolate_token_environment.chmod(0o600)

    loaded_token = init_security()

    assert loaded_token == known_token
    assert get_token_source() == "file"
    assert verify_token(known_token) is True
    assert get_ephemeral_token() == known_token
    assert isolate_token_environment.read_text(encoding="utf-8").strip() == known_token


def test_token_persistence_readonly_filesystem_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Monkeypatch os.open to raise OSError, assert valid 32-byte secret and ephemeral fallback source."""

    def mock_os_open(*args, **kwargs):
        raise OSError("Read-only file system")

    monkeypatch.setattr(os, "open", mock_os_open)

    fallback_token = init_security()

    assert isinstance(fallback_token, str)
    assert len(fallback_token) >= 32
    assert get_token_source() == "generated_ephemeral_fallback"
    assert verify_token(fallback_token) is True
    assert get_ephemeral_token() == fallback_token


def test_auth_rate_limiter_memory_bounding_and_cleanup():
    """Test AuthFailureTracker sliding window, empty key popping, reset on valid auth, and pruning when exceeding max entries."""
    tracker = AuthFailureTracker(max_failures=3, window_seconds=2.0, max_entries=5)
    client_ip = "192.0.2.1"

    # Sliding window: under threshold
    tracker.record_failure(client_ip)
    tracker.record_failure(client_ip)
    assert tracker.is_rate_limited(client_ip) is False

    # Sliding window: reaches threshold
    tracker.record_failure(client_ip)
    assert tracker.is_rate_limited(client_ip) is True
    assert tracker.get_retry_after(client_ip) >= 1

    # Empty key popping when all failure timestamps expire
    tracker._failures[client_ip] = [time.time() - 10.0, time.time() - 9.0, time.time() - 8.0]
    assert client_ip in tracker._failures
    assert tracker.is_rate_limited(client_ip) is False
    assert client_ip not in tracker._failures

    # Reset specific IP on valid auth
    auth_ip = "192.0.2.2"
    tracker.record_failure(auth_ip)
    assert auth_ip in tracker._failures
    tracker.reset(auth_ip)
    assert auth_ip not in tracker._failures

    # Full reset
    tracker.record_failure("192.0.2.3")
    tracker.reset()
    assert len(tracker._failures) == 0

    # Pruning when exceeding max entries
    bounded = AuthFailureTracker(max_failures=5, window_seconds=60.0, max_entries=3)
    bounded.record_failure("10.0.0.1")
    bounded.record_failure("10.0.0.2")
    bounded.record_failure("10.0.0.3")
    assert len(bounded._failures) == 3

    # Exceed max entries: oldest key ("10.0.0.1") is pruned
    bounded.record_failure("10.0.0.4")
    assert len(bounded._failures) <= 3
    assert "10.0.0.1" not in bounded._failures
    assert "10.0.0.4" in bounded._failures

    # Pruning expired entries during record_failure
    bounded._failures["10.0.0.2"] = [time.time() - 120.0]
    bounded.record_failure("10.0.0.5")
    assert "10.0.0.2" not in bounded._failures
    assert "10.0.0.5" in bounded._failures


def test_reverse_proxy_client_ip_headers():
    """Send 10 failed requests with X-Forwarded-For, verify 11th gets 429 with Retry-After, and distinct IP succeeds."""
    app = create_app()
    token = get_ephemeral_token()
    assert token is not None

    client = TestClient(app)
    proxy_headers = {
        "X-Forwarded-For": "203.0.113.195, 10.0.0.1",
        "x-auth-token": "invalid-token",
    }

    # Send 10 failed requests
    for _ in range(10):
        resp = client.get("/api/status", headers=proxy_headers)
        assert resp.status_code == 401
        assert resp.json()["error"] == "UNAUTHORIZED"

    # 11th request returns 429 with Retry-After
    resp_11 = client.get("/api/status", headers=proxy_headers)
    assert resp_11.status_code == 429
    assert resp_11.json()["error"] == "TOO_MANY_REQUESTS"
    assert "Retry-After" in resp_11.headers
    retry_after = int(resp_11.headers["Retry-After"])
    assert retry_after > 0

    # Distinct IP 198.51.100.22 with valid token returns 200 OK without being blocked
    distinct_headers = {
        "X-Forwarded-For": "198.51.100.22",
        "x-auth-token": token,
    }
    resp_distinct = client.get("/api/status", headers=distinct_headers)
    assert resp_distinct.status_code == 200
    assert resp_distinct.json()["status"] == "ok"


def test_health_probe_path_variations_bypass():
    """Block client IP with 10 failed requests. Verify /api/health, /api/health/, and /api/health?probe=1 all return 200 OK."""
    app = create_app()
    client = TestClient(app, follow_redirects=True)

    blocked_ip = "203.0.113.250"
    headers_blocked = {
        "X-Forwarded-For": blocked_ip,
        "x-auth-token": "bad-token",
    }

    # Block client IP with 10 failed requests
    for _ in range(10):
        resp = client.get("/api/status", headers=headers_blocked)
        assert resp.status_code == 401

    # Verify IP is blocked on protected endpoint
    assert client.get("/api/status", headers=headers_blocked).status_code == 429

    probe_headers = {"X-Forwarded-For": blocked_ip}
    expected_body = {"status": "ok", "app": "TeleVault"}

    resp1 = client.get("/api/health", headers=probe_headers)
    assert resp1.status_code == 200
    assert resp1.json() == expected_body

    resp2 = client.get("/api/health/", headers=probe_headers)
    assert resp2.status_code == 200
    assert resp2.json() == expected_body

    resp3 = client.get("/api/health?probe=1", headers=probe_headers)
    assert resp3.status_code == 200
    assert resp3.json() == expected_body


def test_token_persistence_env_var_override(monkeypatch: pytest.MonkeyPatch, isolate_token_environment: Path):
    """Set TELEVAULT_TOKEN='env_secret_token'. Verify token returned is 'env_secret_token', source is 'env', and file is not created."""
    env_secret = "env_secret_token"
    monkeypatch.setenv("TELEVAULT_TOKEN", env_secret)

    token = init_security()

    assert token == env_secret
    assert get_token_source() == "env"
    assert isolate_token_environment.exists() is False
    assert verify_token(env_secret) is True
    assert verify_token("wrong_token") is False


def test_extract_master_token_sanitization():
    """Verify extract_master_token returns None for empty and whitespace strings across all sources."""
    from starlette.requests import Request

    from src.web.client_helpers import extract_master_token

    # Empty string headers
    req_empty = Request({"type": "http", "headers": [(b"x-auth-token", b"")], "query_string": b""})
    assert extract_master_token(req_empty) is None

    # Whitespace headers
    req_space = Request({"type": "http", "headers": [(b"x-auth-token", b"   \t\n")], "query_string": b""})
    assert extract_master_token(req_space) is None

    # Empty query param
    req_qp_empty = Request({"type": "http", "headers": [], "query_string": b"token="})
    assert extract_master_token(req_qp_empty) is None

    # Whitespace query param
    req_qp_space = Request({"type": "http", "headers": [], "query_string": b"token=%20%20"})
    assert extract_master_token(req_qp_space) is None

    # Empty bearer
    req_bearer = Request({"type": "http", "headers": [(b"authorization", b"Bearer ")], "query_string": b""})
    assert extract_master_token(req_bearer) is None

    # Valid token
    req_valid = Request({"type": "http", "headers": [(b"x-auth-token", b"valid_sec_token")], "query_string": b""})
    assert extract_master_token(req_valid) == "valid_sec_token"


def test_empty_token_resilience_and_no_rate_limit(tmp_path: Path):
    """Verify sending X-Auth-Token: '' or ?token= does not return 401 on public endpoints and does not increment rate limit."""
    app = create_app()
    client = TestClient(app)

    visitor_headers = {"X-Forwarded-For": "192.0.2.123", "x-auth-token": "   "}

    # Repeated requests with empty/whitespace token to public endpoint
    for _ in range(15):
        resp = client.get("/api/auth/me", headers=visitor_headers)
        assert resp.status_code == 200
        assert resp.json().get("authorized") is False or resp.json().get("status") in ("unauthenticated", "ok")

    # Verify rate limiter did not record failures for this IP
    resp_health = client.get("/api/health", headers=visitor_headers)
    assert resp_health.status_code == 200

