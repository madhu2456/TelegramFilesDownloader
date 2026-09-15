"""Pure-Python on-the-fly streaming ZIP engine for TeleVault.

Implements PKWARE Bit 3 (0x0008) streaming mode with Method 0 (STORED), 16-byte data
descriptors, exact Content-Length pre-calculation, Zip64 archive structures, and
thread-safe ticket-based download initiation. Zero disk writes, O(1) RAM buffer.
"""
import asyncio
import binascii
import logging
import secrets
import struct
import threading
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def make_dos_time_date(dt: datetime | None = None) -> tuple[int, int]:
    """Convert datetime to MS-DOS (time, date) tuple."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    d_time = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
    d_date = ((dt.year - 1980) << 9) | (dt.month << 5) | dt.day
    return d_time, d_date


@dataclass
class ZipEntryInfo:
    name: str
    filename_bytes: bytes
    crc32: int
    size: int
    offset: int
    dos_time: int
    dos_date: int


def make_local_header(filename_bytes: bytes, dos_time: int, dos_date: int) -> bytes:
    """Create 30-byte local file header + filename using Bit 3 (0x0008) and Bit 11 (0x0800 UTF-8)."""
    # Layout: <4sHHHHHIIIHH
    # Magic (4B), version needed (2B), flags (2B), method (2B), time (2B), date (2B), crc (4B), comp (4B), uncomp (4B), name_len (2B), extra_len (2B)
    header = struct.pack(
        "<4sHHHHHIIIHH",
        b"PK\x03\x04",
        20,      # Version 2.0
        0x0808,  # Bit 3: Data descriptor follows; Bit 11: UTF-8 filename
        0,       # Method 0: STORED (uncompressed)
        dos_time,
        dos_date,
        0,       # CRC-32 (deferred to descriptor)
        0,       # Compressed size (deferred to descriptor)
        0,       # Uncompressed size (deferred to descriptor)
        len(filename_bytes),
        0,       # Extra field length
    )
    return header + filename_bytes


def make_data_descriptor(crc32: int, size: int) -> bytes:
    """Create mandatory 16-byte Data Descriptor with 0x08074b50 signature."""
    # Layout: <4sIII
    # Magic (4B), CRC-32 (4B), Compressed Size (4B), Uncompressed Size (4B)
    return struct.pack(
        "<4sIII",
        b"PK\x07\x08",
        crc32 & 0xFFFFFFFF,
        size & 0xFFFFFFFF,
        size & 0xFFFFFFFF,
    )


def make_central_directory_header(entry: ZipEntryInfo) -> bytes:
    """Create Central Directory header (46 bytes + filename + optional Zip64 extra)."""
    is_offset_64 = entry.offset >= 0xFFFFFFFF
    offset_val = 0xFFFFFFFF if is_offset_64 else entry.offset

    extra_fields = b""
    if is_offset_64:
        # Zip64 Extra Field 0x0001 with 8-byte offset
        extra_fields = struct.pack("<HHQ", 0x0001, 8, entry.offset)

    version_needed = 45 if is_offset_64 else 20

    header = struct.pack(
        "<4sBBHHHHHIIIHHHHHII",
        b"PK\x01\x02",
        20,              # Version made by: 2.0 (MS-DOS / OS-independent)
        3,               # Host system: Unix
        version_needed,  # Version needed to extract
        0x0808,          # Bit 3 (data descriptor), Bit 11 (UTF-8 filename)
        0,               # Method 0: STORED
        entry.dos_time,
        entry.dos_date,
        entry.crc32 & 0xFFFFFFFF,
        entry.size & 0xFFFFFFFF,
        entry.size & 0xFFFFFFFF,
        len(entry.filename_bytes),
        len(extra_fields),
        0,               # File comment length
        0,               # Disk number start
        0,               # Internal file attributes
        0o100644 << 16,  # External file attributes: regular file permissions (rw-r--r--)
        offset_val,
    )
    return header + entry.filename_bytes + extra_fields


def make_eocd_records(entries: list[ZipEntryInfo], cd_offset: int, cd_size: int) -> bytes:
    """Create End of Central Directory record with Zip64 fallback if archive exceeds 4 GiB."""
    entry_count = len(entries)
    is_zip64 = (
        cd_offset >= 0xFFFFFFFF
        or cd_size >= 0xFFFFFFFF
        or entry_count >= 0xFFFF
    )

    records = b""
    if is_zip64:
        # 1. Zip64 End of Central Directory Record (56 bytes)
        # Layout: <4sQHHIIQQQQ
        zip64_eocd = struct.pack(
            "<4sQHHIIQQQQ",
            b"PK\x06\x06",
            44,          # Size of remaining record (56 - 12 = 44 bytes)
            45,          # Version made by: 4.5
            45,          # Version needed: 4.5
            0,           # Disk number
            0,           # Central directory disk
            entry_count, # Entries on this disk
            entry_count, # Total entries
            cd_size,     # Size of central directory
            cd_offset,   # Offset of central directory
        )
        zip64_eocd_offset = cd_offset + cd_size

        # 2. Zip64 End of Central Directory Locator (20 bytes)
        # Layout: <4sIQI
        zip64_locator = struct.pack(
            "<4sIQI",
            b"PK\x06\x07",
            0,                  # Disk with Zip64 EOCD
            zip64_eocd_offset,  # Offset of Zip64 EOCD
            1,                  # Total disks
        )
        records += zip64_eocd + zip64_locator

    # 3. Standard EOCD Record (22 bytes)
    # Layout: <4sHHHHIIH
    standard_eocd = struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06",
        0,                          # Disk number
        0,                          # CD start disk
        min(entry_count, 0xFFFF),   # Entries on this disk
        min(entry_count, 0xFFFF),   # Total entries
        min(cd_size, 0xFFFFFFFF),   # Size of CD
        min(cd_offset, 0xFFFFFFFF), # Offset of CD
        0,                          # Comment length
    )
    records += standard_eocd
    return records


def calculate_archive_size(files: list[dict]) -> int:
    """Pre-calculate the exact byte length of the streaming ZIP archive down to the single byte."""
    offset = 0
    cd_size = 0
    entry_count = len(files)

    for item in files:
        name_bytes = item["name"].encode("utf-8")
        file_size = int(item.get("size", 0) or 0)

        # Local Header (30B) + Filename + Payload + Data Descriptor (16B)
        local_len = 30 + len(name_bytes) + file_size + 16

        # Central Directory Header (46B) + Filename + (12B Zip64 Extra if offset >= 0xFFFFFFFF)
        is_offset_64 = offset >= 0xFFFFFFFF
        cd_len = 46 + len(name_bytes) + (12 if is_offset_64 else 0)

        offset += local_len
        cd_size += cd_len

    is_zip64 = (
        offset >= 0xFFFFFFFF
        or cd_size >= 0xFFFFFFFF
        or entry_count >= 0xFFFF
    )
    eocd_len = (56 + 20 + 22) if is_zip64 else 22

    return offset + cd_size + eocd_len


def deduplicate_filenames(items: list[dict]) -> list[dict]:
    """Ensure all archive entries possess unique filenames, appending message ID on collision."""
    seen: dict[str, int] = {}
    deduped: list[dict] = []

    for item in items:
        name = item.get("name") or f"file_{item.get('msg_id', 0)}"
        if name not in seen:
            seen[name] = 1
            unique_name = name
        else:
            seen[name] += 1
            base, dot, ext = name.rpartition(".")
            ext_str = f".{ext}" if dot else ""
            base_str = base or name
            mid = item.get("msg_id", seen[name])
            unique_name = f"{base_str}_{mid}{ext_str}"

        copy_item = dict(item)
        copy_item["name"] = unique_name
        deduped.append(copy_item)

    return deduped


@dataclass
class ZipTicketInfo:
    ticket: str
    chat_id: int
    chat_title: str
    files: list[dict]
    estimated_archive_size: int
    created_at: float
    redeemed: bool = False
    redeemed_at: float = 0.0


class ZipTicketManager:
    """Thread-safe in-memory cache for short-lived download tickets with 120s TTL."""

    def __init__(self, ttl_seconds: int = 120, max_tickets: int = 200) -> None:
        self._ttl = ttl_seconds
        self._max = max_tickets
        self._tickets: dict[str, ZipTicketInfo] = {}
        self._lock = threading.Lock()

    def create_ticket(self, chat_id: int, chat_title: str, files: list[dict], archive_size: int) -> str:
        with self._lock:
            self._prune_expired()
            if len(self._tickets) >= self._max:
                oldest_key = min(self._tickets, key=lambda k: self._tickets[k].created_at)
                del self._tickets[oldest_key]

            ticket = secrets.token_urlsafe(16)
            self._tickets[ticket] = ZipTicketInfo(
                ticket=ticket,
                chat_id=chat_id,
                chat_title=chat_title,
                files=files,
                estimated_archive_size=archive_size,
                created_at=time.time(),
            )
            return ticket

    def get_ticket(self, ticket: str) -> ZipTicketInfo | None:
        with self._lock:
            self._prune_expired()
            info = self._tickets.get(ticket)
            if not info:
                return None
            info.redeemed = True
            info.redeemed_at = time.time()
            return info

    def _prune_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._tickets.items() if (now - v.created_at) > self._ttl]
        for k in expired:
            del self._tickets[k]


zip_ticket_manager = ZipTicketManager()


class StreamingZipBuilder:
    """Asynchronous on-the-fly streaming ZIP generator."""

    @staticmethod
    async def stream_archive(
        client: Any,
        chat_id: int,
        files: list[dict],
    ) -> AsyncGenerator[bytes, None]:
        """Stream a full ZIP archive from Telethon client chunks without server disk writes."""
        from src.web.client_helpers import _resolve_peer_robust

        entries: list[ZipEntryInfo] = []
        offset = 0
        now = datetime.now(timezone.utc)
        dos_time, dos_date = make_dos_time_date(now)

        try:
            peer = await _resolve_peer_robust(client, chat_id)
            for item in files:
                mid = int(item["msg_id"])
                name = item["name"]
                filename_bytes = name.encode("utf-8")

                # 1. Fetch message media
                try:
                    m = await client.get_messages(peer, ids=mid)
                    if isinstance(m, list):
                        m = m[0] if m else None
                except Exception as fetch_err:
                    logger.warning("Failed to fetch message %s for zip: %s", mid, fetch_err)
                    continue

                if not m or not getattr(m, "media", None):
                    logger.warning("Skipping msg_id %s in zip: media missing", mid)
                    continue

                # 2. Yield Local File Header
                local_header = make_local_header(filename_bytes, dos_time, dos_date)
                yield local_header
                local_header_offset = offset
                offset += len(local_header)

                # 3. Stream binary payload chunks
                crc = 0
                actual_size = 0
                try:
                    async for chunk in client.iter_download(m.media, request_size=512 * 1024):
                        if chunk:
                            yield chunk
                            crc = binascii.crc32(chunk, crc)
                            actual_size += len(chunk)
                            offset += len(chunk)
                except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                    # Downstream client hung up / cancelled: terminate cleanly
                    logger.info("Client disconnected during zip stream of chat %s", chat_id)
                    return
                except Exception as stream_err:
                    logger.warning("Error streaming msg_id %s in zip: %s", mid, stream_err)

                # 4. Yield 16-byte Data Descriptor
                descriptor = make_data_descriptor(crc, actual_size)
                yield descriptor
                offset += len(descriptor)

                # 5. Record entry info for Central Directory
                entries.append(
                    ZipEntryInfo(
                        name=name,
                        filename_bytes=filename_bytes,
                        crc32=crc,
                        size=actual_size,
                        offset=local_header_offset,
                        dos_time=dos_time,
                        dos_date=dos_date,
                    )
                )

        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            logger.info("Downstream client disconnected from zip stream of chat %s", chat_id)
            return

        # 6. Emit Central Directory Headers & EOCD
        try:
            cd_offset = offset
            cd_bytes_list: list[bytes] = []
            for entry in entries:
                cd_header = make_central_directory_header(entry)
                cd_bytes_list.append(cd_header)
                offset += len(cd_header)

            cd_data = b"".join(cd_bytes_list)
            yield cd_data
            cd_size = len(cd_data)

            eocd_records = make_eocd_records(entries, cd_offset, cd_size)
            yield eocd_records
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            return
