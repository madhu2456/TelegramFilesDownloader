"""Unit tests for direct browser scanning and streaming endpoints without disk writes."""
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from src.config import Config
from src.web.app import create_app
from src.web.security import generate_ephemeral_token, set_ephemeral_token


class MockEntity:
    def __init__(self, chat_id=12345, title="Test Channel"):
        self.id = chat_id
        self.title = title
        self.username = "testchannel"
        self.broadcast = True


class MockFile:
    def __init__(self, name="document.pdf", size=1024, mime_type="application/pdf", ext=".pdf"):
        self.name = name
        self.size = size
        self.mime_type = mime_type
        self.ext = ext


class MockMedia:
    pass


class MockMessage:
    def __init__(self, mid, name="document.pdf", size=1024, mime_type="application/pdf", ext=".pdf", is_photo=False, media=True, caption="Test caption"):
        self.id = mid
        self.media = MockMedia() if media else None
        self.file = MockFile(name=name, size=size, mime_type=mime_type, ext=ext) if media else None
        self.photo = MockMedia() if is_photo else None
        self.text = caption
        self.message = caption
        self.date = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
        self.poll = None
        self.contact = None


class MockTgClient:
    def __init__(self, messages=None):
        self.connected = True
        self.messages = messages or []
        self._mock_entity = MockEntity()

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def get_entity(self, target):
        return self._mock_entity

    async def get_input_entity(self, target):
        return self._mock_entity

    async def is_user_authorized(self):
        return True

    def iter_messages(self, entity, limit=100, offset_id=0, search=None):
        async def _gen():
            count = 0
            for m in self.messages:
                if offset_id and m.id >= offset_id:
                    continue
                if search and search.lower() not in (m.text or "").lower():
                    continue
                yield m
                count += 1
                if count >= limit:
                    break
        return _gen()

    async def get_messages(self, entity, ids=None):
        for m in self.messages:
            if m.id == ids:
                return m
        return None

    def iter_download(self, file_media, offset=None, request_size=512 * 1024):
        async def _stream():
            if offset is not None:
                data = b"0123456789" * 500
                start_off = offset or 0
                yield data[start_off:]
            else:
                yield b"TELEVAULT_DIRECT_STREAM_CHUNK_1_"
                yield b"TELEVAULT_DIRECT_STREAM_CHUNK_2_"
        return _stream()


@pytest.fixture
def direct_client(tmp_path):
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path / "out")

    mock_msgs = [
        MockMessage(101, name="report.pdf", size=5000, mime_type="application/pdf", ext=".pdf", caption="Quarterly Report"),
        MockMessage(102, name=None, size=15000, mime_type="image/jpeg", ext=".jpg", is_photo=True, caption="Chart Snapshot"),
        MockMessage(103, name="clip.mp4", size=500000, mime_type="video/mp4", ext=".mp4", caption="Demo Video"),
        MockMessage(104, name="audio.mp3", size=25000, mime_type="audio/mpeg", ext=".mp3", caption="Podcast"),
        MockMessage(105, media=False, caption="Plain text announcement"),
    ]
    app.state.tg_client = MockTgClient(messages=mock_msgs)

    client = TestClient(app)
    return client, token, tmp_path


def test_scan_chat_media(direct_client):
    client, token, tmp_path = direct_client
    res = client.post(
        "/api/chat/scan",
        json={"target": "@testchannel", "limit": 10},
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["chat_id"] == 12345
    assert data["chat_title"] == "Test Channel"
    assert data["count"] == 4  # 4 media messages, 1 plain text skipped

    items = {item["msg_id"]: item for item in data["items"]}
    assert 101 in items
    assert items[101]["name"] == "report.pdf"
    assert items[101]["kind"] == "document"

    assert 102 in items
    assert items[102]["name"] == "photo_12345_102.jpg"
    assert items[102]["kind"] == "photo"

    assert 103 in items
    assert items[103]["kind"] == "video"

    assert 104 in items
    assert items[104]["kind"] == "audio"

    # Confirm no files were written to disk
    out_dir = tmp_path / "out"
    if out_dir.exists():
        assert len(list(out_dir.glob("*"))) == 0


def test_scan_chat_media_filter_kind(direct_client):
    client, token, _ = direct_client
    res = client.post(
        "/api/chat/scan",
        json={"target": "@testchannel", "limit": 10, "filter": "video"},
        headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    assert data["items"][0]["msg_id"] == 103
    assert data["items"][0]["kind"] == "video"


def test_direct_download_streaming_zero_disk(direct_client):
    client, token, tmp_path = direct_client
    res = client.get(
        "/api/direct/download/12345/101",
        headers={"X-Auth-Token": token},
    )
    assert res.status_code == 200
    assert res.headers["Content-Type"] == "application/pdf"
    assert "attachment" in res.headers["Content-Disposition"]
    assert "filename*=UTF-8''report.pdf" in res.headers["Content-Disposition"]
    assert res.content == b"TELEVAULT_DIRECT_STREAM_CHUNK_1_TELEVAULT_DIRECT_STREAM_CHUNK_2_"

    # Confirm zero disk writes occurred during direct streaming
    out_dir = tmp_path / "out"
    if out_dir.exists():
        assert len(list(out_dir.glob("*"))) == 0


def test_direct_download_utf8_filename(direct_client):
    client, token, _ = direct_client
    client.app.state.tg_client.messages.append(
        MockMessage(106, name="Special & Symbols € 2026.pdf", size=1200, mime_type="application/pdf")
    )
    res = client.get(
        "/api/direct/download/12345/106",
        headers={"X-Auth-Token": token},
    )
    assert res.status_code == 200
    cd = res.headers["Content-Disposition"]
    assert "filename*=UTF-8''Special%20%26%20Symbols%20%E2%82%AC%202026.pdf" in cd


def test_direct_download_unauthorized(direct_client):
    client, _, _ = direct_client
    res = client.get("/api/direct/download/12345/101")
    assert res.status_code == 401


def test_direct_download_not_found(direct_client):
    client, token, _ = direct_client
    res = client.get(
        "/api/direct/download/12345/999999",
        headers={"X-Auth-Token": token},
    )
    assert res.status_code == 404


def test_direct_download_range_start_end(direct_client):
    client, token, _ = direct_client
    res = client.get(
        "/api/direct/download/12345/101",
        headers={"X-Auth-Token": token, "Range": "bytes=0-499"},
    )
    assert res.status_code == 206
    assert res.headers["Content-Range"] == "bytes 0-499/5000"
    assert res.headers["Content-Length"] == "500"
    assert len(res.content) == 500


def test_direct_download_range_suffix(direct_client):
    client, token, _ = direct_client
    res = client.get(
        "/api/direct/download/12345/101",
        headers={"X-Auth-Token": token, "Range": "bytes=-4000"},
    )
    assert res.status_code == 206
    assert res.headers["Content-Range"] == "bytes 1000-4999/5000"
    assert res.headers["Content-Length"] == "4000"
    assert res.content == (b"0123456789" * 500)[1000:5000]


def test_direct_download_range_unsatisfiable(direct_client):
    client, token, _ = direct_client
    res = client.get(
        "/api/direct/download/12345/101",
        headers={"X-Auth-Token": token, "Range": "bytes=5000-6000"},
    )
    assert res.status_code == 416
    assert res.headers["Content-Range"] == "bytes */5000"

def test_direct_download_range_single_byte(direct_client):
    client, token, _ = direct_client
    res = client.get(
        "/api/direct/download/12345/101",
        headers={"X-Auth-Token": token, "Range": "bytes=0-0"},
    )
    assert res.status_code == 206
    assert res.headers["Content-Range"] == "bytes 0-0/5000"
    assert res.headers["Content-Length"] == "1"
    assert res.content == b"0"
