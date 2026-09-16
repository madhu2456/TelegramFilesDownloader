"""Tests for TeleVault Web Dashboard (Core Engine, Security, JobManager, Media, and Static Assets)."""
import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from telethon.errors import SessionPasswordNeededError

from src.config import Config
from src.store import init_db, record_download
from src.web.app import create_app
from src.web.job_manager import JobConflictError, JobManager
from src.web.routes_media import classify_mime, parse_range_header
from src.web.security import (
    generate_ephemeral_token,
    get_ephemeral_token,
    mask_phone,
    verify_origin,
    verify_token,
)


def test_security_token_generation_and_verification():
    token = generate_ephemeral_token()
    assert len(token) >= 32
    assert get_ephemeral_token() == token
    assert verify_token(token) is True
    assert verify_token("invalid_token_12345") is False
    assert verify_token("") is False


def test_security_origin_verification():
    assert verify_origin("http://127.0.0.1:8000", 8000) is True
    assert verify_origin("http://localhost:8000", 8000) is True
    assert verify_origin("http://[::1]:8000", 8000) is True
    assert verify_origin("http://127.0.0.1:9000", 8000) is False
    assert verify_origin("http://attacker.com", 8000) is False
    assert verify_origin("http://evil-localhost:8000", 8000) is False
    assert verify_origin(None, 8000) is True


def test_security_mask_phone():
    assert mask_phone("+15551234567") == "+1***4567"
    assert mask_phone("+919876543210") == "+9***3210"
    assert mask_phone("1234") == "1***4"
    assert mask_phone("") == ""


def test_app_status_auth_and_origin(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    # 1. Unauthenticated request -> 401
    resp = client.get("/api/status")
    assert resp.status_code == 401
    assert "UNAUTHORIZED" in resp.text

    # 2. Authenticated request with X-Auth-Token header -> 200
    resp = client.get("/api/status", headers={"X-Auth-Token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["phone_masked"] == "+1***4567"
    assert "abcdef0123456789" not in str(data)

    # 3. Authenticated request via query param -> 200
    resp = client.get(f"/api/status?token={token}")
    assert resp.status_code == 200

    # 4. State-changing POST with foreign origin -> 403
    resp = client.post(
        "/api/download/cancel",
        headers={"X-Auth-Token": token, "Origin": "http://evil.com"},
    )
    assert resp.status_code == 403


def test_api_health_unauthenticated(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg, out_dir=tmp_path)
    client = TestClient(app)

    # Health check is open and unauthenticated
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "app": "TeleVault"}

    # Status remains protected
    status_resp = client.get("/api/status")
    assert status_resp.status_code == 401
    assert "UNAUTHORIZED" in status_resp.text



def test_job_manager_singleton_mutex_and_circular_buffer():
    jm = JobManager()
    assert jm.is_running() is False
    assert len(jm.get_recent_logs()) == 0

    # Fill circular buffer past maxlen (1000)
    for i in range(1200):
        jm.add_log(f"log message {i}")
    recent = jm.get_recent_logs()
    assert len(recent) == 1000
    assert "log message 200" in recent[0]
    assert "log message 1199" in recent[-1]

    # Simulate running job
    jm._is_running = True
    jm._active_job_id = "job-42"
    assert jm.is_running() is True
    snap = jm.get_snapshot()
    assert snap["status"] == "running"
    assert snap["job_id"] == "job-42"

    async def run_conflict_test():
        with pytest.raises(JobConflictError) as exc_info:
            await jm.start_job(None, None, None, None, None)
        assert "already running" in str(exc_info.value)

    asyncio.run(run_conflict_test())


def test_download_start_auto_terminates_previous_job(tmp_path: Path):
    """Starting a new download auto-terminates the previous active job instead of 409."""
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    # Simulate a running job that cancel_job_async can clear
    jm = app.state.job_manager
    jm._is_running = True
    jm._active_job_id = "active-job"
    jm._cancel_event.clear()

    resp = client.post(
        "/api/download/start",
        json={"target": "@testchannel"},
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    # After auto-termination, the job manager should have cancelled the previous job
    # The request may fail at target resolution (no real Telegram client), but should NOT be 409
    assert resp.status_code != 409


def test_classify_mime_logic():
    assert classify_mime("video/mp4", "video.mp4") == "video"
    assert classify_mime("video/webm", "test.webm") == "video"
    assert classify_mime(None, "movie.mkv") == "video"

    assert classify_mime("image/jpeg", "pic.jpg") == "photo"
    assert classify_mime("image/png", "pic.png") == "photo"
    assert classify_mime(None, "photo.webp") == "photo"

    assert classify_mime("audio/mpeg", "song.mp3") == "audio"
    assert classify_mime("audio/ogg", "track.ogg") == "audio"
    assert classify_mime(None, "sound.flac") == "audio"

    assert classify_mime("application/pdf", "doc.pdf") == "document"
    assert classify_mime("text/plain", "notes.txt") == "document"
    assert classify_mime(None, "archive.tar.gz") == "document"


def test_parse_range_header_rfc7233():
    total = 2048
    start, end = parse_range_header("bytes=0-1023", total)
    assert start == 0 and end == 1023

    start, end = parse_range_header("bytes=1000-", total)
    assert start == 1000 and end == 2047

    start, end = parse_range_header("bytes=-500", total)
    assert start == 1548 and end == 2047

    start, end = parse_range_header(None, total)
    assert start == 0 and end == 2047

    with pytest.raises(HTTPException) as exc:
        parse_range_header("bytes=3000-", total)
    assert exc.value.status_code == 416
    assert exc.value.headers.get("Content-Range") == f"bytes */{total}"

    with pytest.raises(HTTPException) as exc:
        parse_range_header("bytes=500-200", total)
    assert exc.value.status_code == 416


def test_media_stream_traversal_protection(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    resp = client.get(
        "/api/media/stream/123/456",
        headers={"X-Auth-Token": token},
    )
    assert resp.status_code == 404


def test_static_assets_integrity():
    static_dir = Path("src/web/static")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    styles_css = (static_dir / "styles.css").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")

    assert "<meta name=\"referrer\" content=\"no-referrer\">" in index_html
    assert "TeleVault" in index_html
    assert "speedGauge" in index_html
    assert "terminalLogs" in index_html
    assert "mediaGrid" in index_html

    assert "#0B0E14" in styles_css
    assert "backdrop-filter" in styles_css
    assert "#00E5FF" in styles_css

    assert "history.replaceState" in app_js
    assert "/ws/live" in app_js
    assert "copyToClipboard" in app_js


def test_get_media_populated_with_filename_and_relpath(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    conn = init_db(tmp_path / "manifest.db")
    record_download(
        conn,
        12345,
        1,
        "a" * 64,
        1024,
        "sub/video.mp4",
        meta={
            "mime": "video/mp4",
            "date_utc": "2026-09-11 12:00:00",
            "snippet": "A test video",
        },
    )
    conn.close()

    sub_dir = tmp_path / "sub"
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / "video.mp4").write_bytes(b"dummy video data")

    resp = client.get(f"/api/media?token={token}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["filename"] == "video.mp4"
    assert item["relpath"] == "sub/video.mp4"
    assert item["exists"] is True
    assert item["kind"] == "video"
    assert item["mime"] == "video/mp4"
    assert item["stream_url"] == "/api/media/stream/12345/1"
    assert data["pagination"]["total"] == 1


def test_get_media_legacy_schema_migration_and_compatibility(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    db_path = tmp_path / "manifest.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE downloads(chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, "
        "sha256 TEXT NOT NULL, size INTEGER NOT NULL, relpath TEXT NOT NULL, "
        "created_at TEXT DEFAULT (datetime('now')), UNIQUE(chat_id,msg_id));"
    )
    conn.execute(
        "INSERT INTO downloads(chat_id, msg_id, sha256, size, relpath) VALUES (?, ?, ?, ?, ?)",
        (999, 1, "b" * 64, 2048, "legacy.jpg"),
    )
    conn.commit()
    conn.close()

    resp = client.get(f"/api/media?token={token}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["filename"] == "legacy.jpg"
    assert item["relpath"] == "legacy.jpg"
    assert item["date_utc"] is None
    assert item["kind"] == "photo"


def test_stream_media_success_and_rfc7233_range(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    conn = init_db(tmp_path / "manifest.db")
    record_download(
        conn,
        111,
        2,
        "c" * 64,
        100,
        "sample.bin",
        meta={"mime": "application/octet-stream"},
    )
    conn.close()

    (tmp_path / "sample.bin").write_bytes(bytes(range(100)))

    resp = client.get(
        "/api/media/stream/111/2",
        headers={"X-Auth-Token": token, "Range": "bytes=10-29"},
    )
    assert resp.status_code == 206
    assert resp.headers.get("Content-Range") == "bytes 10-29/100"
    assert resp.headers.get("Content-Length") == "20"
    assert resp.content == bytes(range(10, 30))


def test_stream_media_file_deleted_returns_404(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    conn = init_db(tmp_path / "manifest.db")
    record_download(conn, 111, 3, "d" * 64, 50, "missing.bin")
    conn.close()

    missing_file = tmp_path / "missing.bin"
    if missing_file.exists():
        missing_file.unlink()

    resp = client.get(
        "/api/media/stream/111/3",
        headers={"X-Auth-Token": token},
    )
    assert resp.status_code == 404
    assert "Media file not found on disk" in resp.json().get("detail", "")


def test_stream_media_traversal_returns_403(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    conn = init_db(tmp_path / "manifest.db")
    record_download(conn, 111, 4, "e" * 64, 50, "../../etc/shadow")
    conn.close()

    resp = client.get(
        "/api/media/stream/111/4",
        headers={"X-Auth-Token": token},
    )
    assert resp.status_code == 403
    assert "Forbidden: path outside output directory" in resp.json().get("detail", "")


def test_stream_media_nonexistent_manifest_or_item_returns_404(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    # Case 1: Missing manifest DB -> 404
    resp = client.get(
        "/api/media/stream/999/1",
        headers={"X-Auth-Token": token},
    )
    assert resp.status_code == 404
    assert "Manifest database not found" in resp.json().get("detail", "")

    # Case 2: Missing chat_id/msg_id in manifest -> 404
    conn = init_db(tmp_path / "manifest.db")
    conn.close()

    resp2 = client.get(
        "/api/media/stream/999/1",
        headers={"X-Auth-Token": token},
    )
    assert resp2.status_code == 404
    assert "Media item not found in manifest" in resp2.json().get("detail", "")


def test_dialogs_endpoint_unauthorized():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/api/dialogs")
    assert resp.status_code == 401
    assert "UNAUTHORIZED" in resp.text


def test_dialogs_endpoint_client_uninitialized(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.tg_client = None
    client = TestClient(app)
    token = get_ephemeral_token()
    resp = client.get("/api/dialogs", headers={"X-Auth-Token": token})
    assert resp.status_code == 500
    assert "Telegram client not initialized" in resp.json().get("detail", "")


def test_dialogs_endpoint_normalization_dual_keys(tmp_path: Path, monkeypatch):

    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    app.state.tg_client = mock_client
    client = TestClient(app)
    token = get_ephemeral_token()

    sample_dialogs = [
        {"name": "Tech Channel", "id": 101, "type": "channel", "handle": "@techchannel"},
        {"name": "Study Group", "id": 202, "type": "group", "handle": ""},
        {"name": "Alice", "id": 303, "type": "dm", "handle": "@alice"},
    ]

    async def mock_fetch_dialog_rows(_client, limit=100, kind="all"):
        return sample_dialogs

    monkeypatch.setattr("src.web.routes_tg.fetch_dialog_rows", mock_fetch_dialog_rows)

    resp = client.get(f"/api/dialogs?kind=all&token={token}")
    assert resp.status_code == 200
    dialogs = resp.json()["dialogs"]
    assert len(dialogs) == 3

    assert dialogs[0]["type"] == "channel"
    assert dialogs[0]["kind"] == "channel"
    assert dialogs[0]["handle"] == "@techchannel"
    assert dialogs[0]["username"] == "techchannel"

    assert dialogs[1]["type"] == "group"
    assert dialogs[1]["kind"] == "group"
    assert dialogs[1]["handle"] == ""
    assert dialogs[1]["username"] is None

    assert dialogs[2]["type"] == "dm"
    assert dialogs[2]["kind"] == "dm"
    assert dialogs[2]["handle"] == "@alice"
    assert dialogs[2]["username"] == "alice"


def test_dialogs_endpoint_server_search_parity(tmp_path: Path, monkeypatch):

    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    app.state.tg_client = mock_client
    client = TestClient(app)
    token = get_ephemeral_token()

    sample_dialogs = [
        {"name": "Python Devs", "id": 501, "type": "channel", "handle": "@python_devs"},
        {"name": "General Chat", "id": 502, "type": "group", "handle": ""},
        {"name": "Bob", "id": 503, "type": "dm", "handle": "@bobsmith"},
    ]

    async def mock_fetch_dialog_rows(_client, limit=100, kind="all"):
        return sample_dialogs

    monkeypatch.setattr("src.web.routes_tg.fetch_dialog_rows", mock_fetch_dialog_rows)

    # search=python -> 1 item (Python Devs)
    r1 = client.get(f"/api/dialogs?search=python&token={token}")
    assert r1.status_code == 200
    res1 = r1.json()["dialogs"]
    assert len(res1) == 1
    assert res1[0]["name"] == "Python Devs"

    # search=@bobsmith -> 1 item (Bob)
    r2 = client.get(f"/api/dialogs?search=@bobsmith&token={token}")
    assert r2.status_code == 200
    res2 = r2.json()["dialogs"]
    assert len(res2) == 1
    assert res2[0]["name"] == "Bob"

    # search=bobsmith -> 1 item (Bob)
    r3 = client.get(f"/api/dialogs?search=bobsmith&token={token}")
    assert r3.status_code == 200
    res3 = r3.json()["dialogs"]
    assert len(res3) == 1
    assert res3[0]["name"] == "Bob"

    # search=502 -> 1 item (General Chat)
    r4 = client.get(f"/api/dialogs?search=502&token={token}")
    assert r4.status_code == 200
    res4 = r4.json()["dialogs"]
    assert len(res4) == 1
    assert res4[0]["name"] == "General Chat"

    # search=nonexistent -> 0 items
    r5 = client.get(f"/api/dialogs?search=nonexistent&token={token}")
    assert r5.status_code == 200
    res5 = r5.json()["dialogs"]
    assert len(res5) == 0

def test_ws_live_authenticated_success(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    client = TestClient(app)
    token = get_ephemeral_token()

    with client.websocket_connect(f"/ws/live?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "INIT_STATE"
        assert "state" in msg
        assert "logs" in msg
        assert isinstance(msg["logs"], list)


def test_ws_live_invalid_token_clean_1008(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    client = TestClient(app)

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/ws/live?token=invalid_token_xyz"),
    ):
        pass
    assert exc_info.value.code == 1008


def test_ws_live_invalid_origin_clean_1008(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    client = TestClient(app)
    token = get_ephemeral_token()

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(
            f"/ws/live?token={token}",
            headers={"origin": "http://evil-domain.com:8000"},
        ),
    ):
        pass
    assert exc_info.value.code == 1008


def test_stream_media_unicode_rfc5987_content_disposition(tmp_path: Path):
    """Verify non-ASCII filenames stream cleanly with RFC 5987 content-disposition header and support download=1 attachment mode."""
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    client = TestClient(app)
    token = get_ephemeral_token()

    unicode_filename = "видео_वीडियो_動画.mp4"
    conn = init_db(tmp_path / "manifest.db")
    record_download(conn, 888, 1, "f" * 64, 100, unicode_filename, meta={"mime": "video/mp4"})
    conn.close()

    (tmp_path / unicode_filename).write_bytes(b"dummy byte content")

    # Inline streaming check
    resp = client.get(f"/api/media/stream/888/1?token={token}")
    assert resp.status_code == 200
    cd = resp.headers.get("Content-Disposition", "")
    assert "inline;" in cd
    assert "filename*=UTF-8''" in cd

    # Attachment download check
    resp_dl = client.get(f"/api/media/stream/888/1?token={token}&download=1")
    assert resp_dl.status_code == 200
    assert "attachment;" in resp_dl.headers.get("Content-Disposition", "")


def test_auth_me_scenarios(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    client = TestClient(app)
    token = get_ephemeral_token()

    # Case 1: tg_client is None
    app.state.tg_client = None
    r1 = client.get(f"/api/auth/me?token={token}")
    assert r1.status_code == 200
    assert r1.json() == {"authorized": False}

    # Case 2: tg_client is not authorized
    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.is_user_authorized.return_value = False
    app.state.tg_client = mock_client
    r2 = client.get(f"/api/auth/me?token={token}")
    assert r2.status_code == 200
    assert r2.json() == {"authorized": False}

    # Case 3: tg_client is authorized
    mock_client.is_user_authorized.return_value = True
    mock_me = SimpleNamespace(
        id=777,
        first_name="John",
        last_name="Doe",
        username="johndoe",
        phone="+15559876543",
    )
    mock_client.get_me = AsyncMock(return_value=mock_me)
    r3 = client.get(f"/api/auth/me?token={token}")
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["authorized"] is True
    assert d3["user"]["id"] == 777
    assert d3["user"]["name"] == "John Doe"
    assert d3["user"]["username"] == "johndoe"
    assert d3["user"]["phone"] == "+1***6543"

    # Case 4: Exception during check returns authorized: False with error string
    mock_client.is_user_authorized.side_effect = RuntimeError("Network down")
    r4 = client.get(f"/api/auth/me?token={token}")
    assert r4.status_code == 200
    assert r4.json()["authorized"] is False
    assert "Network down" in r4.json()["error"]


def test_auth_qr_and_phone_login_flow(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    token = get_ephemeral_token()

    # QR: client is None -> 500
    app.state.tg_client = None
    c = TestClient(app)
    r_qr_none = c.get(f"/api/auth/qr?token={token}")
    assert r_qr_none.status_code == 500

    # QR: client connected and returns QR
    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_qr = SimpleNamespace(
        token="test_qr_token",
        url="tg://login?token=xyz",
        expires=datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    mock_client.qr_login = AsyncMock(return_value=mock_qr)
    app.state.tg_client = mock_client
    r_qr_ok = c.get(f"/api/auth/qr?token={token}")
    assert r_qr_ok.status_code == 200
    assert r_qr_ok.json()["token"] == "test_qr_token"
    assert r_qr_ok.json()["url"] == "tg://login?token=xyz"

    # Phone send_code: Invalid phone format -> 400
    r_bad_phone = c.post(
        "/api/auth/phone/send_code",
        json={"phone": "12345"},
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert r_bad_phone.status_code == 400
    assert "Invalid E.164" in r_bad_phone.text

    # Phone send_code: Valid E.164
    mock_client.send_code_request = AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash_123"))
    r_send_ok = c.post(
        "/api/auth/phone/send_code",
        json={"phone": "+15551234567"},
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert r_send_ok.status_code == 200
    assert r_send_ok.json()["status"] == "code_sent"
    assert r_send_ok.json()["phone_code_hash"] == "hash_123"

    # Phone sign_in: 2FA required (SessionPasswordNeededError)
    mock_client.sign_in = AsyncMock(side_effect=SessionPasswordNeededError(None))
    r_2fa_req = c.post(
        "/api/auth/phone/sign_in",
        json={"phone": "+15551234567", "code": "12345"},
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert r_2fa_req.status_code == 200
    assert r_2fa_req.json() == {"status": "2fa_required"}

    # Phone sign_in: Success
    mock_client.sign_in = AsyncMock(return_value=None)
    r_sign_ok = c.post(
        "/api/auth/phone/sign_in",
        json={"phone": "+15551234567", "code": "12345"},
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert r_sign_ok.status_code == 200
    assert r_sign_ok.json() == {"status": "authorized"}

    # 2FA endpoint: Success
    r_2fa_ok = c.post(
        "/api/auth/2fa",
        json={"password": "mypassword"},
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert r_2fa_ok.status_code == 200
    assert r_2fa_ok.json() == {"status": "authorized"}

    # 2FA endpoint: Invalid password -> 400
    mock_client.sign_in = AsyncMock(side_effect=ValueError("Password incorrect"))
    r_2fa_fail = c.post(
        "/api/auth/2fa",
        json={"password": "wrongpassword"},
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert r_2fa_fail.status_code == 400
    assert "Password incorrect" in r_2fa_fail.text


def test_api_resolve_target_endpoint(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    token = get_ephemeral_token()
    c = TestClient(app)

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    app.state.tg_client = mock_client

    with patch("src.web.routes_tg.resolve_target") as mock_resolve:
        mock_resolve.return_value = SimpleNamespace(
            entity=SimpleNamespace(id=987, title="My Channel"),
            kind="channel",
            value="mychannel",
        )
        resp = c.post(
            "/api/resolve",
            json={"target": "@mychannel"},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 987
        assert data["title"] == "My Channel"
        assert data["kind"] == "channel"
        assert data["is_participant"] is True

        # Error branch -> 400
        mock_resolve.side_effect = RuntimeError("Channel does not exist")
        resp_err = c.post(
            "/api/resolve",
            json={"target": "@notfound"},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert resp_err.status_code == 400
        assert "Cannot resolve target" in resp_err.text


def test_system_storage_and_download_cancel_routes(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path)
    token = get_ephemeral_token()
    c = TestClient(app)

    # Storage endpoint
    r_storage = c.get(f"/api/system/storage?token={token}")
    assert r_storage.status_code == 200
    st = r_storage.json()
    assert "total_bytes" in st
    assert "free_bytes" in st
    assert "used_percent" in st
    assert "is_low_space" in st

    # Cancel endpoint when idle -> no_active_job
    r_cancel_idle = c.post(
        "/api/download/cancel",
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert r_cancel_idle.status_code == 200
    assert r_cancel_idle.json() == {"status": "no_active_job"}

    # Cancel endpoint when running -> cancelling
    app.state.job_manager._is_running = True
    app.state.job_manager._active_job_id = "job-active"
    r_cancel_active = c.post(
        "/api/download/cancel",
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert r_cancel_active.status_code == 200
    assert r_cancel_active.json() == {"status": "cancelling"}
    assert app.state.job_manager._cancel_event.is_set()

    # State snapshot endpoint
    r_state = c.get(f"/api/download/state?token={token}")
    assert r_state.status_code == 200
    state_data = r_state.json()
    assert "snapshot" in state_data
    assert "logs" in state_data


def test_job_manager_flood_wait_and_broadcast():
    jm = JobManager()
    q1 = jm.subscribe()
    q2 = jm.subscribe()

    jm.set_flood_wait(45)
    snap = jm.get_snapshot()
    assert snap["flood_wait_seconds"] == 45
    assert any("FloodWait 45s" in log for log in jm.get_recent_logs())

    evt1 = q1.get_nowait()
    assert evt1["type"] == "LOG"
    evt2 = q1.get_nowait()
    assert evt2["type"] == "FLOOD_WAIT"
    assert evt2["seconds"] == 45

    jm.unsubscribe(q1)
    jm.add_log("another message")
    # q1 is unsubscribed and should be empty
    assert q1.empty()
    # q2 receives the event
    assert not q2.empty()


def test_job_manager_run_job_failure_state(tmp_path: Path):
    jm = JobManager()
    conn = init_db(":memory:")
    q = jm.subscribe()

    async def run_failure_job():
        with patch("src.downloader.download_chat", side_effect=RuntimeError("Download aborted unexpectedly")):
            job_id = await jm.start_job(None, "@dummy", SimpleNamespace(limit=10), tmp_path, conn)
            # Wait for the async task to finish
            await asyncio.gather(jm._active_task, return_exceptions=True)
            return job_id

    asyncio.run(run_failure_job())
    snap = jm.get_snapshot()
    assert snap["status"] == "failed"
    assert jm.is_running() is False
    assert any("error: Download aborted unexpectedly" in log for log in jm.get_recent_logs())

    # Verify broadcast of failure
    received_types = []
    while not q.empty():
        received_types.append(q.get_nowait()["type"])
    assert "FAILED" in received_types


def test_ui_tooltips_structural_audit():
    """Verify DOM structure, IDs, accessibility roles, and decoupling for all 19 tooltips."""
    from html.parser import HTMLParser
    from typing import Any

    class TooltipDOMParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.tag_stack: list[str] = []
            self.tooltips: list[dict[str, str | None]] = []
            self.triggers: list[dict[str, Any]] = []
            self.triggers_inside_label: list[dict[str, Any]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attr_dict = dict(attrs)
            classes = (attr_dict.get("class") or "").split()

            if "tooltip-bubble" in classes:
                self.tooltips.append({
                    "id": attr_dict.get("id"),
                    "role": attr_dict.get("role"),
                    "class": attr_dict.get("class"),
                })

            if "tooltip-trigger" in classes:
                trigger_info = {
                    "tag": tag,
                    "type": attr_dict.get("type"),
                    "aria_label": attr_dict.get("aria-label"),
                    "aria_describedby": attr_dict.get("aria-describedby"),
                    "inside_label": "label" in self.tag_stack,
                }
                self.triggers.append(trigger_info)
                if "label" in self.tag_stack:
                    self.triggers_inside_label.append(trigger_info)

            if tag.lower() not in (
                "area", "base", "br", "col", "embed", "hr", "img",
                "input", "link", "meta", "param", "source", "track", "wbr",
            ):
                self.tag_stack.append(tag.lower())

        def handle_endtag(self, tag: str) -> None:
            tag_lower = tag.lower()
            if tag_lower in self.tag_stack:
                while self.tag_stack:
                    popped = self.tag_stack.pop()
                    if popped == tag_lower:
                        break

    html_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "index.html"
    assert html_path.exists(), f"File not found: {html_path}"
    content = html_path.read_text(encoding="utf-8")

    parser = TooltipDOMParser()
    parser.feed(content)

    expected_ids = {
        "tip-phone", "tip-code", "tip-target", "tip-limit", "tip-no-limit",
        "tip-filter", "tip-search", "tip-after", "tip-before", "tip-from-user",
        "tip-sync", "tip-resume", "tip-dry-run", "tip-takeout", "tip-join",
        "tip-2fa", "tip-modal-target", "tip-modal-search", "tip-select-all",
    }

    assert len(parser.tooltips) == 19, f"Expected 19 tooltips, found {len(parser.tooltips)}"
    found_ids = {t["id"] for t in parser.tooltips}
    assert found_ids == expected_ids, (
        f"Tooltip ID mismatch. Missing: {expected_ids - found_ids}, Extra: {found_ids - expected_ids}"
    )
    for t in parser.tooltips:
        assert t["role"] == "tooltip", f"Tooltip {t['id']} does not have role='tooltip': {t}"
    assert len(parser.triggers) == 19, f"Expected 19 triggers, found {len(parser.triggers)}"
    for tr in parser.triggers:
        assert tr["tag"] == "button", f"Trigger tag is not button: {tr}"
        assert tr["type"] == "button", f"Trigger missing type='button': {tr}"
        assert tr["aria_label"] and tr["aria_label"].strip(), f"Trigger missing non-empty aria-label: {tr}"
        assert tr["aria_describedby"] in expected_ids, f"Trigger aria-describedby not in expected IDs: {tr}"
    assert len(parser.triggers_inside_label) == 0, (
        f"Found tooltip triggers nested inside <label>: {parser.triggers_inside_label}"
    )


def test_production_host_and_origin_validation(tmp_path: Path):
    cfg = Config(
        api_id=12345,
        api_hash="abcdef0123456789abcdef0123456789",
        phone="+15551234567",
        session_path=tmp_path / "test.session",
    )
    app = create_app(cfg)
    client = TestClient(app)

    # 1. Host header validation
    # Allowed host: televault.madhudadi.in
    res = client.get("/api/status", headers={"Host": "televault.madhudadi.in"})
    assert res.status_code in (200, 401)

    # Disallowed host returns 400 Invalid host header
    res = client.get("/api/status", headers={"Host": "evil.com"})
    assert res.status_code == 400

    # 2. Origin verification function
    assert verify_origin("https://televault.madhudadi.in", 8000) is True
    assert verify_origin("http://televault.madhudadi.in", 8000) is True
    assert verify_origin("https://televault.madhudadi.in:443", 8000) is True
    assert verify_origin("https://evil.com", 8000) is False
    assert verify_origin("https://sub.televault.madhudadi.in", 8000) is False

    # 3. WebSocket origin check with production domain
    token = get_ephemeral_token()
    with client.websocket_connect(
        f"/ws/live?token={token}",
        headers={"Origin": "https://televault.madhudadi.in"}
    ) as ws:
        data = ws.receive_json()
        assert data["type"] == "INIT_STATE"


def test_direct_streaming_ui_and_storage_pill_removal():
    """Verify #storagePill is absent from HTML, updateStorage short-circuits in JS, and direct streaming copy is active."""
    static_dir = Path(__file__).resolve().parent.parent / "src" / "web" / "static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    js = (static_dir / "app.js").read_text(encoding="utf-8")
    assert 'id="storagePill"' not in html
    assert "storagePill" in js
    assert "if (!pill) return;" in js
    assert "Direct Browser Streaming Active" in js
    assert "Browse &amp; Download Files" in html

