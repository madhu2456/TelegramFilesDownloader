"""Tests for TeleVault Web Dashboard (Core Engine, Security, JobManager, Media, and Static Assets)."""
from starlette.websockets import WebSocketDisconnect
import asyncio
from pathlib import Path
import sqlite3
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.config import Config
from src.store import init_db, record_download
from src.web.app import create_app
from src.web.job_manager import JobManager, JobConflictError
from src.web.security import (
    generate_ephemeral_token,
    verify_token,
    verify_origin,
    mask_phone,
    get_ephemeral_token,
)
from src.web.routes_media import classify_mime, parse_range_header


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
    from unittest.mock import MagicMock

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
    from unittest.mock import MagicMock

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

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/live?token=invalid_token_xyz") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "SESSION_EXPIRED"
            assert "Session token invalid" in msg["detail"]
            ws.receive_json()
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

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"/ws/live?token={token}",
            headers={"origin": "http://evil-domain.com:8000"},
        ) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "ORIGIN_FORBIDDEN"
            assert "Origin not allowed" in msg["detail"]
            ws.receive_json()
    assert exc_info.value.code == 1008

