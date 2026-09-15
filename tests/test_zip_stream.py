"""Complete test suite for on-the-fly streaming ZIP engine and two-phase download endpoints."""
import asyncio
import io
import time
import zipfile
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import Config
from src.web.app import create_app
from src.web.security import generate_ephemeral_token, set_ephemeral_token
from src.web.zip_stream import (
    StreamingZipBuilder,
    ZipTicketManager,
    calculate_archive_size,
    deduplicate_filenames,
    make_dos_time_date,
)

pytestmark = pytest.mark.anyio


class MockFile:
    def __init__(self, name: str | None = "document.pdf", size: int = 1024, ext: str = ".pdf"):
        self.name = name
        self.size = size
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
        ext: str = ".pdf",
    ):
        self.id = mid
        self.chunks = chunks if chunks is not None else [b"chunk_1", b"chunk_2"]
        size = sum(len(c) for c in self.chunks)
        self.media = MockMedia(self.chunks) if media else None
        self.file = MockFile(name=name, size=size, ext=ext) if media else None
        self.photo = MockMedia(self.chunks) if is_photo else None
        self.text = f"Message {mid}"
        self.poll = None
        self.contact = None


class MockTgClient:
    def __init__(self, messages: list[MockMessage] | None = None):
        self.connected = True
        self.messages = messages or []
        self._msgs_by_id = {m.id: m for m in self.messages}

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

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


async def test_zip_stream_binary_integrity() -> None:
    """Test PKWARE Bit 3 streaming ZIP binary integrity, CRC-32 validation, and data descriptors."""
    file_specs = [
        {"mid": 1, "name": "document.txt", "chunks": [b"Hello ", b"world! ", b"Part 1 of zip test.\n"]},
        {"mid": 2, "name": "data/binary.dat", "chunks": [b"\x00\x01\x02\x03", b"\x04\x05\x06\x07", b"\x08\x09\x0a\x0b"]},
        {"mid": 3, "name": "images/photo.png", "chunks": [b"\x89PNG\r\n\x1a\n", b"\x00\x00\x00\rIHDR", b"pixel_data_here"]},
    ]
    messages = [
        MockMessage(
            mid=spec["mid"],
            name=spec["name"],
            chunks=spec["chunks"],
        )
        for spec in file_specs
    ]
    client = MockTgClient(messages=messages)
    files = [
        {"msg_id": s["mid"], "name": s["name"], "size": sum(len(c) for c in s["chunks"])}
        for s in file_specs
    ]

    accumulated_bytes = bytearray()
    async for chunk in StreamingZipBuilder.stream_archive(client, chat_id=12345, files=files):
        accumulated_bytes.extend(chunk)

    all_bytes = bytes(accumulated_bytes)
    assert len(all_bytes) > 0

    with zipfile.ZipFile(io.BytesIO(all_bytes)) as zf:
        # zf.testzip() returns None if all CRCs, local headers, and descriptors match
        assert zf.testzip() is None

        # Assert all file names exist and contents match exact source chunks
        namelist = zf.namelist()
        assert len(namelist) == 3
        for spec in file_specs:
            assert spec["name"] in namelist
            expected_content = b"".join(spec["chunks"])
            assert zf.read(spec["name"]) == expected_content


async def test_zip_archive_exact_size_calculation() -> None:
    """Test 100% exact parity between pre-calculated archive size and actual streamed bytes."""
    file_specs = [
        {"mid": 10, "name": "first_report.pdf", "chunks": [b"A" * 1500, b"B" * 2500]},
        {"mid": 11, "name": "nested/subfolder/file_two.csv", "chunks": [b"col1,col2\n", b"val1,val2\n", b"val3,val4\n"]},
        {"mid": 12, "name": "utf8_üñîçødë_名前.dat", "chunks": [b"\xaa\xbb\xcc" * 200]},
    ]
    messages = [
        MockMessage(mid=s["mid"], name=s["name"], chunks=s["chunks"])
        for s in file_specs
    ]
    client = MockTgClient(messages=messages)
    files = [
        {"msg_id": s["mid"], "name": s["name"], "size": sum(len(c) for c in s["chunks"])}
        for s in file_specs
    ]

    calculated_size = calculate_archive_size(files)

    accumulated = bytearray()
    async for chunk in StreamingZipBuilder.stream_archive(client, chat_id=98765, files=files):
        accumulated.extend(chunk)

    # 100% exact byte-for-byte parity for browser Content-Length header
    assert len(accumulated) == calculated_size

    # Verify structural integrity
    with zipfile.ZipFile(io.BytesIO(accumulated)) as zf:
        assert zf.testzip() is None
        assert len(zf.namelist()) == 3


def test_zip_deduplication() -> None:
    """Test deduplicate_filenames preserves extensions and resolves collisions uniquely."""
    raw_files = [
        {"msg_id": 101, "name": "photo.jpg", "size": 1000},
        {"msg_id": 102, "name": "photo.jpg", "size": 2000},
        {"msg_id": 103, "name": "photo.jpg", "size": 3000},
        {"msg_id": 104, "name": "archive.tar.gz", "size": 4000},
        {"msg_id": 105, "name": "archive.tar.gz", "size": 5000},
        {"msg_id": 106, "name": "no_extension", "size": 6000},
        {"msg_id": 107, "name": "no_extension", "size": 7000},
        {"msg_id": 108, "name": "", "size": 8000},
    ]

    deduped = deduplicate_filenames(raw_files)

    # All resulting filenames must be strictly unique
    deduped_names = [f["name"] for f in deduped]
    assert len(deduped_names) == len(set(deduped_names))

    # Check preserved extensions and collision handling
    assert deduped[0]["name"] == "photo.jpg"
    assert deduped[1]["name"] == "photo_102.jpg"
    assert deduped[2]["name"] == "photo_103.jpg"

    assert deduped[3]["name"] == "archive.tar.gz"
    assert deduped[4]["name"] == "archive.tar_105.gz"

    assert deduped[5]["name"] == "no_extension"
    assert deduped[6]["name"] == "no_extension_107"

    # Missing name fallback
    assert deduped[7]["name"] == "file_108"

    # Confirm original list was not mutated in place
    assert raw_files[1]["name"] == "photo.jpg"


def test_zip_ticket_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test ZipTicketManager ticket creation, retrieval, and TTL expiration."""
    current_time = 1000.0
    monkeypatch.setattr(time, "time", lambda: current_time)

    mgr = ZipTicketManager(ttl_seconds=120, max_tickets=5)
    files = [{"msg_id": 1, "name": "file.txt", "size": 100}]

    # 1. Create ticket
    ticket = mgr.create_ticket(
        chat_id=1234,
        chat_title="My Test Chat",
        files=files,
        archive_size=250,
    )
    assert isinstance(ticket, str) and len(ticket) > 0

    # 2. Retrieve ticket
    info = mgr.get_ticket(ticket)
    assert info is not None
    assert info.ticket == ticket
    assert info.chat_id == 1234
    assert info.chat_title == "My Test Chat"
    assert info.files == files
    assert info.estimated_archive_size == 250
    assert info.redeemed is True
    assert info.redeemed_at == current_time

    # Nonexistent ticket returns None
    assert mgr.get_ticket("nonexistent-ticket-id") is None

    # 3. TTL expiration: within TTL (60s passed)
    current_time += 60.0
    assert mgr.get_ticket(ticket) is not None

    # Beyond TTL (> 120s from creation, now at +121s)
    current_time += 61.0
    assert mgr.get_ticket(ticket) is None

    # 4. Max tickets capacity eviction
    current_time = 2000.0
    mgr_cap = ZipTicketManager(ttl_seconds=300, max_tickets=2)
    t1 = mgr_cap.create_ticket(1, "c1", [], 10)
    current_time += 1.0
    t2 = mgr_cap.create_ticket(2, "c2", [], 20)
    current_time += 1.0
    t3 = mgr_cap.create_ticket(3, "c3", [], 30)

    # t1 should have been evicted as the oldest ticket
    assert mgr_cap.get_ticket(t1) is None
    assert mgr_cap.get_ticket(t2) is not None
    assert mgr_cap.get_ticket(t3) is not None


async def test_api_zip_prepare_and_stream(tmp_path: Any) -> None:
    """Test POST /api/direct/zip/prepare and GET /api/direct/zip/stream/{ticket} two-phase flow."""
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.out_dir = str(tmp_path / "out")

    msg101_chunks = [b"Chunk1A_", b"Chunk1B_"]
    msg102_chunks = [b"Chunk2A_", b"Chunk2B_", b"Chunk2C_"]

    mock_msgs = [
        MockMessage(101, name="first.txt", chunks=msg101_chunks, ext=".txt"),
        MockMessage(102, name="second.bin", chunks=msg102_chunks, ext=".bin"),
    ]
    app.state.tg_client = MockTgClient(messages=mock_msgs)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Phase 1: POST /api/direct/zip/prepare
        prepare_res = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 777, "msg_ids": [101, 102], "chat_title": "Telegram Channel"},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert prepare_res.status_code == 200
        data = prepare_res.json()
        assert "ticket" in data
        assert data["file_count"] == 2
        assert data["total_bytes"] == sum(len(c) for c in msg101_chunks + msg102_chunks)
        assert data["archive_size"] > 0
        assert data["suggested_filename"] == "Telegram_Channel_archive.zip"

        ticket = data["ticket"]
        expected_archive_size = data["archive_size"]

        # Phase 2: GET /api/direct/zip/stream/{ticket}
        stream_res = await client.get(
            f"/api/direct/zip/stream/{ticket}",
            headers={"X-Auth-Token": token},
        )
        assert stream_res.status_code == 200

        # Verify headers
        assert stream_res.headers["Content-Type"] == "application/zip"
        assert stream_res.headers["Content-Length"] == str(expected_archive_size)
        assert "attachment" in stream_res.headers["Content-Disposition"]
        assert "Telegram_Channel_archive.zip" in stream_res.headers["Content-Disposition"]

        # Verify streaming completes and exact content length parity
        body = stream_res.content
        assert len(body) == int(stream_res.headers["Content-Length"])
        assert len(body) == expected_archive_size

        # Verify binary integrity
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            assert zf.testzip() is None
            assert sorted(zf.namelist()) == ["first.txt", "second.bin"]
            assert zf.read("first.txt") == b"".join(msg101_chunks)
            assert zf.read("second.bin") == b"".join(msg102_chunks)


async def test_zero_server_disk_writes(tmp_path: Any) -> None:
    """Verify that during zip preparation and streaming, no files are written to server disk."""
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.out_dir = str(out_dir)

    mock_msgs = [
        MockMessage(201, name="file1.log", chunks=[b"log line 1\n", b"log line 2\n"], ext=".log"),
        MockMessage(202, name="file2.log", chunks=[b"log line 3\n", b"log line 4\n"], ext=".log"),
    ]
    app.state.tg_client = MockTgClient(messages=mock_msgs)

    # Initial state: out_dir is empty
    assert len(list(out_dir.glob("**/*"))) == 0

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Prepare
        prep_res = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 888, "msg_ids": [201, 202], "chat_title": "DiskWriteTest"},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert prep_res.status_code == 200
        ticket = prep_res.json()["ticket"]

        # Stream
        stream_res = await client.get(
            f"/api/direct/zip/stream/{ticket}",
            headers={"X-Auth-Token": token},
        )
        assert stream_res.status_code == 200
        assert len(stream_res.content) > 0

    # Strict assertion: no files or subdirectories created anywhere on server disk
    assert len(list(out_dir.glob("**/*"))) == 0


async def test_api_zip_prepare_empty_ids(tmp_path: Any) -> None:
    """Test POST /api/direct/zip/prepare with empty msg_ids returns 400 Bad Request."""
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.tg_client = MockTgClient([])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 999, "msg_ids": [], "chat_title": "EmptyTest"},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert res.status_code == 400
        assert "No message IDs provided" in res.json()["detail"]


async def test_api_zip_prepare_no_downloadable_media(tmp_path: Any) -> None:
    """Test POST /api/direct/zip/prepare when messages have no media returns 404."""
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.tg_client = MockTgClient([MockMessage(301, media=False)])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post(
            "/api/direct/zip/prepare",
            json={"chat_id": 999, "msg_ids": [301], "chat_title": "NoMedia"},
            headers={"X-Auth-Token": token, "Origin": "http://127.0.0.1:8000"},
        )
        assert res.status_code == 404
        assert "No downloadable media" in res.json()["detail"]


async def test_api_zip_stream_invalid_ticket(tmp_path: Any) -> None:
    """Test GET /api/direct/zip/stream/{ticket} with invalid ticket returns 404."""
    token = generate_ephemeral_token()
    set_ephemeral_token(token)

    cfg = Config(api_id=12345, api_hash="mock", phone="+15551234567", session_path=tmp_path / "sess.session")
    app = create_app(cfg)
    app.state.tg_client = MockTgClient([])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get(
            "/api/direct/zip/stream/invalid-ticket-key",
            headers={"X-Auth-Token": token},
        )
        assert res.status_code == 404
        assert "expired or invalid" in res.json()["detail"]


async def test_zip_stream_skip_missing_media() -> None:
    """Verify stream_archive gracefully skips messages where media is missing or fetch fails."""
    valid_chunks = [b"valid_content"]
    messages = [
        MockMessage(mid=401, name="valid.txt", chunks=valid_chunks),
        MockMessage(mid=402, media=False),  # Missing media
    ]
    client = MockTgClient(messages=messages)
    files = [
        {"msg_id": 401, "name": "valid.txt", "size": sum(len(c) for c in valid_chunks)},
        {"msg_id": 402, "name": "missing.txt", "size": 0},
        {"msg_id": 403, "name": "nonexistent.txt", "size": 0},  # Fetch returns None
    ]

    accumulated = bytearray()
    async for chunk in StreamingZipBuilder.stream_archive(client, chat_id=111, files=files):
        accumulated.extend(chunk)

    with zipfile.ZipFile(io.BytesIO(accumulated)) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == ["valid.txt"]
        assert zf.read("valid.txt") == b"".join(valid_chunks)


async def test_zip_stream_client_disconnect() -> None:
    """Verify stream_archive terminates cleanly when client disconnects during chunk streaming."""
    class DisconnectingTgClient(MockTgClient):
        async def iter_download(self, file_media: Any, request_size: int = 512 * 1024) -> AsyncGenerator[bytes, None]:
            yield b"first_chunk"
            raise asyncio.CancelledError()

    client = DisconnectingTgClient(messages=[MockMessage(501, name="disconnect.bin", chunks=[b"data"])])
    files = [{"msg_id": 501, "name": "disconnect.bin", "size": 4}]

    chunks_received = []
    # Should catch CancelledError internally and return without throwing
    async for chunk in StreamingZipBuilder.stream_archive(client, chat_id=222, files=files):
        chunks_received.append(chunk)

    # Received the local header and first chunk before disconnect
    assert len(chunks_received) >= 2


def test_make_dos_time_date() -> None:
    """Test MS-DOS date and time conversion helper."""
    dt = datetime(2026, 9, 14, 11, 45, 30, tzinfo=timezone.utc)
    dos_time, dos_date = make_dos_time_date(dt)

    # MS-DOS time: (hour << 11) | (minute << 5) | (second // 2)
    expected_time = (11 << 11) | (45 << 5) | (30 // 2)
    # MS-DOS date: ((year - 1980) << 9) | (month << 5) | day
    expected_date = ((2026 - 1980) << 9) | (9 << 5) | 14

    assert dos_time == expected_time
    assert dos_date == expected_date

    # Default dt (None) returns valid non-zero tuple
    d_time, d_date = make_dos_time_date(None)
    assert d_time > 0
    assert d_date > 0


async def test_resolve_peer_robust() -> None:
    """Test _resolve_peer_robust direct return, candidate fallbacks, negative ID, and graceful fallback."""
    from telethon.tl.types import PeerChannel, PeerChat

    from src.web.client_helpers import _resolve_peer_robust

    # 1. Normal entity / direct return
    direct_entity = object()

    class DirectClient:
        async def get_input_entity(self, peer: Any) -> Any:
            if peer == 12345:
                return direct_entity
            raise ValueError(f"Unknown {peer}")

        async def get_entity(self, peer: Any) -> Any:
            raise ValueError("get_entity should not be called")

    assert await _resolve_peer_robust(DirectClient(), 12345) is direct_entity

    # 2. Positive channel ID fallback to -100{id}, PeerChannel, PeerChat
    # 2a. Fallback to -100{id}
    neg_entity = object()

    class FallbackNegClient:
        async def get_input_entity(self, peer: Any) -> Any:
            if peer == -10012345:
                return neg_entity
            raise ValueError(f"Direct failed for {peer}")

        async def get_entity(self, peer: Any) -> Any:
            raise ValueError("Not found")

    assert await _resolve_peer_robust(FallbackNegClient(), 12345) is neg_entity

    # 2b. Fallback to PeerChannel(id)
    peer_channel_entity = object()

    class FallbackPeerChannelClient:
        async def get_input_entity(self, peer: Any) -> Any:
            if isinstance(peer, PeerChannel) and peer.channel_id == 12345:
                return peer_channel_entity
            raise ValueError(f"Failed for {peer}")

        async def get_entity(self, peer: Any) -> Any:
            raise ValueError("Not found")

    assert await _resolve_peer_robust(FallbackPeerChannelClient(), 12345) is peer_channel_entity

    # 2c. Fallback to PeerChat(id)
    peer_chat_entity = object()

    class FallbackPeerChatClient:
        async def get_input_entity(self, peer: Any) -> Any:
            if isinstance(peer, PeerChat) and peer.chat_id == 12345:
                return peer_chat_entity
            raise ValueError(f"Failed for {peer}")

        async def get_entity(self, peer: Any) -> Any:
            raise ValueError("Not found")

    assert await _resolve_peer_robust(FallbackPeerChatClient(), 12345) is peer_chat_entity

    # 3. Negative ID resolution
    from telethon import utils

    resolved_entity = object()
    test_neg_id = -1001987654321
    raw_channel_id = utils.resolve_id(test_neg_id)[0]

    class NegativeIdClient:
        async def get_input_entity(self, peer: Any) -> Any:
            if peer == raw_channel_id:
                return resolved_entity
            raise ValueError(f"Failed for {peer}")

        async def get_entity(self, peer: Any) -> Any:
            raise ValueError("Not found")

    assert await _resolve_peer_robust(NegativeIdClient(), test_neg_id) is resolved_entity

    # 4. Graceful fallback
    # 4a. Client is None
    assert await _resolve_peer_robust(None, 55555) == 55555

    # 4b. All get_input_entity and get_entity calls fail
    class TotalFailureClient:
        async def get_input_entity(self, peer: Any) -> Any:
            raise RuntimeError("Network error")

        async def get_entity(self, peer: Any) -> Any:
            raise RuntimeError("Database error")

    assert await _resolve_peer_robust(TotalFailureClient(), 77777) == 77777
    assert await _resolve_peer_robust(TotalFailureClient(), "@unresolvable_channel") == "@unresolvable_channel"
