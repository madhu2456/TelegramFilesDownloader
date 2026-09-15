"""End-to-End (E2E) integration test suite for TeleVault Direct Browser Download and Streaming ZIP."""
import io
import zipfile
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import Config
from src.web.app import create_app
from src.web.security import generate_ephemeral_token, set_ephemeral_token
from src.web.zip_stream import StreamingZipBuilder

pytestmark = pytest.mark.anyio


class MockEntity:
    def __init__(self, chat_id: int = 12345, title: str = "Test Channel", username: str = "testchannel"):
        self.id = chat_id
        self.title = title
        self.username = username
        self.broadcast = True


class MockFile:
    def __init__(
        self,
        name: str | None = "document.pdf",
        size: int = 1024,
        mime_type: str = "application/pdf",
        ext: str = ".pdf",
    ):
        self.name = name
        self.size = size
        self.mime_type = mime_type
        self.ext = ext


class MockMedia:
    def __init__(self, chunks: list[bytes] | None = None):
        self.chunks = chunks if chunks is not None else []


class MockMessage:
    def __init__(
        self,
        mid: int,
        name: str | None = "document.pdf",
        chunks: list[bytes] | None = None,
        is_photo: bool = False,
        media: bool = True,
        mime_type: str = "application/pdf",
        ext: str = ".pdf",
        caption: str = "Test caption",
    ):
        self.id = mid
        self.chunks = chunks if chunks is not None else [b"chunk_1", b"chunk_2"]
        size = sum(len(c) for c in self.chunks)
        self.media = MockMedia(self.chunks) if media else None
        self.file = MockFile(name=name, size=size, mime_type=mime_type, ext=ext) if media else None
        self.photo = MockMedia(self.chunks) if is_photo else None
        self.text = caption
        self.message = caption
        self.date = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
        self.poll = None
        self.contact = None


class MockTgClient:
    def __init__(self, messages: list[MockMessage] | None = None, entity: MockEntity | None = None):
        self.connected = True
        self.messages = messages or []
        self._mock_entity = entity or MockEntity()
        self._msgs_by_id = {m.id: m for m in self.messages}

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def get_entity(self, target: Any) -> MockEntity:
        return self._mock_entity

    async def get_input_entity(self, target: Any) -> MockEntity:
        return self._mock_entity

    async def is_user_authorized(self) -> bool:
        return True

    def iter_messages(
        self,
        entity: Any,
        limit: int = 100,
        offset_id: int = 0,
        search: str | None = None,
    ) -> AsyncGenerator[MockMessage, None]:
        async def _gen() -> AsyncGenerator[MockMessage, None]:
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

    async def get_messages(self, entity: Any, ids: int | list[int] | None = None) -> Any:
        if isinstance(ids, list):
            return [self._msgs_by_id[i] for i in ids if i in self._msgs_by_id]
        if ids in self._msgs_by_id:
            return self._msgs_by_id[ids]
        return None

    async def iter_download(self, file_media: Any, request_size: int = 512 * 1024) -> AsyncGenerator[bytes, None]:
        chunks = getattr(file_media, "chunks", [])
        for c in chunks:
            yield c


async def test_e2e_full_scan_and_zip_journey(tmp_path: Any) -> None:
    """E2E Journey: scan chat with pagination, select mixed media, prepare ticket, stream ZIP, verify zero disk writes."""
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.out_dir = str(out_dir)

    # 4 distinct media types + 1 plain text announcement (in descending ID order)
    mock_msgs = [
        MockMessage(
            104,
            name="clip.mp4",
            chunks=[b"VIDEO_HEADER_", b"VIDEO_FRAMES_"],
            mime_type="video/mp4",
            ext=".mp4",
            caption="Demo Video",
        ),
        MockMessage(
            103,
            name="photo.jpg",
            chunks=[b"JPEG_EXIF_", b"JPEG_IMAGE_DATA_"],
            is_photo=True,
            mime_type="image/jpeg",
            ext=".jpg",
            caption="Snapshot Photo",
        ),
        MockMessage(
            102,
            name="podcast.mp3",
            chunks=[b"ID3_TAGS_", b"AUDIO_STREAM_DATA_"],
            mime_type="audio/mpeg",
            ext=".mp3",
            caption="Weekly Podcast",
        ),
        MockMessage(
            101,
            name="report.pdf",
            chunks=[b"%PDF-1.4_", b"DOCUMENT_PAGES_"],
            mime_type="application/pdf",
            ext=".pdf",
            caption="Quarterly Report",
        ),
        MockMessage(
            100,
            media=False,
            caption="Plain text announcement without media",
        ),
    ]
    app.state.tg_client = MockTgClient(messages=mock_msgs)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Step 1: Scan chat with pagination (page 1: limit=2)
        scan_p1 = await client.post(
            "/api/chat/scan",
            json={"target": "@testchannel", "limit": 2},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert scan_p1.status_code == 200
        p1_data = scan_p1.json()
        assert p1_data["chat_id"] == 12345
        assert p1_data["chat_title"] == "Test Channel"
        assert p1_data["count"] == 2
        assert p1_data["has_more"] is True
        assert p1_data["next_offset_id"] == 103

        # Step 2: Scan page 2 using next_offset_id
        scan_p2 = await client.post(
            "/api/chat/scan",
            json={"target": "@testchannel", "limit": 2, "offset_id": p1_data["next_offset_id"]},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert scan_p2.status_code == 200
        p2_data = scan_p2.json()
        assert p2_data["count"] == 2

        all_scanned_items = p1_data["items"] + p2_data["items"]
        assert len(all_scanned_items) == 4
        kinds_found = {item["kind"] for item in all_scanned_items}
        assert kinds_found == {"video", "photo", "audio", "document"}

        # Step 3: Select all 4 media items across different kinds
        selected_ids = [item["msg_id"] for item in all_scanned_items]
        assert sorted(selected_ids) == [101, 102, 103, 104]

        # Step 4: Pre-flight ZIP ticket via POST /api/direct/zip/prepare
        prep_res = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 12345, "msg_ids": selected_ids, "chat_title": "Test Channel"},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert prep_res.status_code == 200
        prep_data = prep_res.json()
        assert "ticket" in prep_data
        assert prep_data["file_count"] == 4
        assert prep_data["archive_size"] > 0
        assert prep_data["suggested_filename"] == "Test_Channel_archive.zip"
        ticket = prep_data["ticket"]
        expected_size = prep_data["archive_size"]

        # Step 5: Stream the ZIP archive via GET /api/direct/zip/stream/{ticket}
        stream_res = await client.get(
            f"/api/direct/zip/stream/{ticket}",
            headers={"X-Auth-Token": token},
        )
        assert stream_res.status_code == 200

        # Step 6: Verify headers
        assert stream_res.headers["Content-Type"] == "application/zip"
        assert stream_res.headers["Content-Length"] == str(expected_size)
        assert "attachment" in stream_res.headers["Content-Disposition"]
        assert "Test_Channel_archive.zip" in stream_res.headers["Content-Disposition"]
        assert stream_res.headers["X-Content-Type-Options"] == "nosniff"

        # Step 7: Verify byte stream length exactly equals Content-Length
        zip_bytes = stream_res.content
        assert len(zip_bytes) == int(stream_res.headers["Content-Length"])
        assert len(zip_bytes) == expected_size

        # Step 8: Unpack the streamed ZIP in memory and verify integrity & contents
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.testzip() is None
            names = zf.namelist()
            assert len(names) == 4
            assert "clip.mp4" in names
            assert "photo_12345_103.jpg" in names or "photo.jpg" in names
            assert "podcast.mp3" in names
            assert "report.pdf" in names

            assert zf.read("clip.mp4") == b"VIDEO_HEADER_VIDEO_FRAMES_"
            assert zf.read("podcast.mp3") == b"ID3_TAGS_AUDIO_STREAM_DATA_"
            assert zf.read("report.pdf") == b"%PDF-1.4_DOCUMENT_PAGES_"

        # Step 9: Verify zero server disk writes
        assert len(list(out_dir.glob("**/*"))) == 0


async def test_e2e_edge_cases_zip(tmp_path: Any) -> None:
    """E2E Edge cases: zero-byte files, UTF-8 unicode names, deduplication collisions, weird extensions, disconnect."""
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.out_dir = str(out_dir)

    mock_msgs = [
        # Zero-byte file
        MockMessage(201, name="empty.txt", chunks=[], mime_type="text/plain", ext=".txt"),
        # Unicode / UTF-8 filenames
        MockMessage(202, name="日本語_ビデオ.mp4", chunks=[b"JAPANESE_VIDEO_PAYLOAD"], mime_type="video/mp4", ext=".mp4"),
        MockMessage(203, name="café_résumé.pdf", chunks=[b"CAFE_RESUME_PAYLOAD"], mime_type="application/pdf", ext=".pdf"),
        MockMessage(204, name="emoji_🚀_photo.jpg", chunks=[b"EMOJI_ROCKET_PHOTO"], is_photo=True, mime_type="image/jpeg", ext=".jpg"),
        # Duplicate filenames with different msg_ids
        MockMessage(205, name="video.mp4", chunks=[b"VIDEO_ONE"], mime_type="video/mp4", ext=".mp4"),
        MockMessage(206, name="video.mp4", chunks=[b"VIDEO_TWO"], mime_type="video/mp4", ext=".mp4"),
        MockMessage(207, name="video.mp4", chunks=[b"VIDEO_THREE"], mime_type="video/mp4", ext=".mp4"),
        # Weird extensions & no extensions
        MockMessage(208, name="archive.tar.gz", chunks=[b"GZ_TARBALL"], mime_type="application/gzip", ext=".tar.gz"),
        MockMessage(209, name="no_ext_file", chunks=[b"RAW_BINARY_NO_EXT"], mime_type="application/octet-stream", ext=""),
    ]
    app.state.tg_client = MockTgClient(messages=mock_msgs)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        prep_res = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 9999, "msg_ids": [201, 202, 203, 204, 205, 206, 207, 208, 209], "chat_title": "EdgeCases"},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert prep_res.status_code == 200
        prep_data = prep_res.json()
        assert prep_data["file_count"] == 9
        ticket = prep_data["ticket"]

        stream_res = await client.get(
            f"/api/direct/zip/stream/{ticket}",
            headers={"X-Auth-Token": token},
        )
        assert stream_res.status_code == 200
        assert len(stream_res.content) == int(stream_res.headers["Content-Length"])

        with zipfile.ZipFile(io.BytesIO(stream_res.content)) as zf:
            assert zf.testzip() is None
            names = zf.namelist()

            # Zero-byte file present and empty
            assert "empty.txt" in names
            assert zf.read("empty.txt") == b""

            # Unicode names present and intact
            assert "日本語_ビデオ.mp4" in names
            assert zf.read("日本語_ビデオ.mp4") == b"JAPANESE_VIDEO_PAYLOAD"
            assert "café_résumé.pdf" in names
            assert zf.read("café_résumé.pdf") == b"CAFE_RESUME_PAYLOAD"
            assert "emoji_🚀_photo.jpg" in names
            assert zf.read("emoji_🚀_photo.jpg") == b"EMOJI_ROCKET_PHOTO"

            # Duplicate filenames disambiguated uniquely preserving extension
            assert "video.mp4" in names
            assert "video_206.mp4" in names
            assert "video_207.mp4" in names
            assert zf.read("video.mp4") == b"VIDEO_ONE"
            assert zf.read("video_206.mp4") == b"VIDEO_TWO"
            assert zf.read("video_207.mp4") == b"VIDEO_THREE"

            # Weird and missing extensions
            assert "archive.tar.gz" in names
            assert zf.read("archive.tar.gz") == b"GZ_TARBALL"
            assert "no_ext_file" in names
            assert zf.read("no_ext_file") == b"RAW_BINARY_NO_EXT"

        assert len(list(out_dir.glob("**/*"))) == 0

    # Client disconnect mid-stream: connection drops after 1st chunk
    class DisconnectingClient(MockTgClient):
        async def iter_download(self, file_media: Any, request_size: int = 512 * 1024) -> AsyncGenerator[bytes, None]:
            yield b"first_chunk_received"
            raise ConnectionResetError("Downstream client hung up")

    disconnect_client = DisconnectingClient(messages=[MockMessage(301, name="drop.bin", chunks=[b"data"])])
    chunks_collected = []
    async for chunk in StreamingZipBuilder.stream_archive(
        disconnect_client,
        chat_id=9999,
        files=[{"msg_id": 301, "name": "drop.bin", "size": 4}],
    ):
        chunks_collected.append(chunk)

    # Clean termination without hanging or raising unhandled exception
    assert len(chunks_collected) >= 1


async def test_e2e_auth_and_security_edge_cases(tmp_path: Any) -> None:
    """E2E Security & Auth: 401 unauthenticated, 404 expired ticket, 403 cross-origin, 400 empty IDs, 404 no media."""
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.tg_client = MockTgClient(messages=[MockMessage(401, media=False, caption="Text only")])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. POST /api/direct/zip/prepare without auth token -> 401
        res_unauth = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 12345, "msg_ids": [401]},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert res_unauth.status_code == 401

        # 2. GET /api/direct/zip/stream/{ticket} with expired or nonexistent ticket -> 404
        res_bad_ticket = await client.get(
            "/api/direct/zip/stream/nonexistent_or_expired_ticket_123",
            headers={"X-Auth-Token": token},
        )
        assert res_bad_ticket.status_code == 404
        assert "expired or invalid" in res_bad_ticket.json()["detail"]

        # 3. Cross-origin request rejection on POST endpoints without valid Origin header -> 403
        res_bad_origin = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 12345, "msg_ids": [401]},
            headers={"X-Auth-Token": token, "Origin": "http://malicious-site.com:8000"},
        )
        assert res_bad_origin.status_code == 403
        assert res_bad_origin.json()["error"] == "FORBIDDEN"

        # 4. Empty msg_ids list -> 400
        res_empty = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 12345, "msg_ids": []},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert res_empty.status_code == 400
        assert "No message IDs provided" in res_empty.json()["detail"]

        # 5. Chat with no media in requested IDs -> 404
        res_no_media = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 12345, "msg_ids": [401]},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert res_no_media.status_code == 404
        assert "No downloadable media found" in res_no_media.json()["detail"]


async def test_e2e_single_file_direct_stream(tmp_path: Any) -> None:
    """E2E Single file direct stream: headers, zero disk writes, 404 on non-existent message."""
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.out_dir = str(out_dir)

    payload = b"DIRECT_CHUNK_A_DIRECT_CHUNK_B_"
    app.state.tg_client = MockTgClient(messages=[
        MockMessage(
            501,
            name="document.pdf",
            chunks=[b"DIRECT_CHUNK_A_", b"DIRECT_CHUNK_B_"],
            mime_type="application/pdf",
            ext=".pdf",
        )
    ])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Success streaming
        res = await client.get(
            "/api/direct/download/12345/501",
            headers={"X-Auth-Token": token},
        )
        assert res.status_code == 200
        assert res.headers["Accept-Ranges"] == "bytes"
        assert res.headers["Content-Type"] == "application/pdf"
        assert "attachment" in res.headers["Content-Disposition"]
        assert "filename*=UTF-8''document.pdf" in res.headers["Content-Disposition"]
        assert res.headers["Content-Length"] == str(len(payload))
        assert res.content == payload

        # Zero server disk writes
        assert len(list(out_dir.glob("**/*"))) == 0

        # Non-existent message -> 404
        res_missing = await client.get(
            "/api/direct/download/12345/99999",
            headers={"X-Auth-Token": token},
        )
        assert res_missing.status_code == 404
