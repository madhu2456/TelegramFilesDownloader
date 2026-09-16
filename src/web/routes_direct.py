"""Direct browser extraction and streaming routes without server disk writes."""

import asyncio
from collections.abc import AsyncGenerator
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from telethon import utils

from src.resolver import resolve_target
from src.web.client_helpers import (
    _ensure_connected,
    _resolve_peer_robust,
    get_session_client,
)
from src.web.routes_media import classify_mime
from src.web.zip_stream import (
    StreamingZipBuilder,
    calculate_archive_size,
    deduplicate_filenames,
    zip_ticket_manager,
)

router = APIRouter()


class ChatScanRequest(BaseModel):
    target: str
    limit: int = 100
    filter: str | None = None
    search: str | None = None
    offset_id: int = 0
    join: bool = False


@router.post("/api/chat/scan")
async def scan_chat_media(req: ChatScanRequest, request: Request):
    """Scan messages in target entity and extract media metadata without server disk writes."""
    client = await get_session_client(request, allow_jit=False)
    if not client:
        raise HTTPException(status_code=401, detail="Telegram client not initialized or session not authenticated")

    await _ensure_connected(client)

    try:
        resolved = await resolve_target(client, req.target, join=req.join)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Target resolution failed: {e}")

    ent = getattr(resolved, "entity", resolved)
    try:
        chat_id = utils.get_peer_id(ent)
    except Exception:
        chat_id = int(getattr(ent, "id", 0) or 0)
    chat_title = (
        getattr(ent, "title", None)
        or getattr(ent, "username", None)
        or getattr(ent, "first_name", None)
        or str(chat_id)
    )

    eff_limit = min(max(1, req.limit), 500)
    items: list[dict] = []
    last_msg_id: int | None = None
    total_scanned = 0

    filter_val = (req.filter or "").strip().lower()
    if filter_val == "all":
        filter_val = ""

    async for m in client.iter_messages(
        ent,
        limit=eff_limit,
        offset_id=req.offset_id or 0,
        search=req.search or None,
    ):
        total_scanned += 1
        mid = int(getattr(m, "id", 0) or 0)
        last_msg_id = mid

        med = getattr(m, "media", None)
        if not med:
            continue

        med_type = type(med).__name__
        if med_type in (
            "MessageMediaPoll",
            "MessageMediaContact",
            "MessageMediaUnsupported",
            "MessageMediaGame",
            "MessageMediaInvoice",
            "MessageMediaGeo",
            "MessageMediaVenue",
        ):
            continue
        if getattr(m, "poll", None) or getattr(m, "contact", None):
            continue

        f = getattr(m, "file", None)
        is_photo = bool(getattr(m, "photo", None) or med_type == "MessageMediaPhoto")
        if f is None and not is_photo:
            continue

        raw_name = getattr(f, "name", None) if f else None
        ext = getattr(f, "ext", "") if f else ""
        if is_photo or (not raw_name and not ext):
            ext = ext or ".jpg"
            filename = raw_name or f"photo_{chat_id}_{mid}{ext}"
        else:
            ext = ext or ""
            filename = raw_name or f"file_{chat_id}_{mid}{ext}"

        size = int(getattr(f, "size", 0) or 0) if f else 0
        mime = getattr(f, "mime_type", None) if f else None
        if not mime or mime == "application/octet-stream":
            mime = "image/jpeg" if is_photo else "application/octet-stream"

        kind = classify_mime(mime, filename)

        if filter_val:
            kind_match = kind == filter_val
            type_match = filter_val in med_type.lower()
            attr_match = bool(getattr(m, filter_val, None) or getattr(med, filter_val, None))
            if not (kind_match or type_match or attr_match):
                continue

        date_utc = None
        m_date = getattr(m, "date", None)
        if m_date:
            date_utc = m_date.isoformat() if hasattr(m_date, "isoformat") else str(m_date)

        caption = getattr(m, "text", None) or getattr(m, "message", None) or ""
        caption = caption[:200] if isinstance(caption, str) and caption else ""

        items.append(
            {
                "msg_id": mid,
                "chat_id": chat_id,
                "name": filename,
                "size": size,
                "mime": mime,
                "kind": kind,
                "date": date_utc,
                "caption": caption,
            }
        )

    has_more = total_scanned >= eff_limit
    next_offset_id = last_msg_id if has_more else None

    return {
        "items": items,
        "count": len(items),
        "has_more": has_more,
        "next_offset_id": next_offset_id,
        "chat_id": chat_id,
        "chat_title": str(chat_title),
    }


@router.get("/api/direct/download/{chat_id}/{msg_id}")
async def direct_stream_media(request: Request, chat_id: int, msg_id: int):
    """Stream media directly from Telegram into HTTP client without server disk writes."""
    client = await get_session_client(request, allow_jit=False)
    if not client:
        raise HTTPException(status_code=401, detail="Telegram client not initialized or session not authenticated")

    await _ensure_connected(client)

    try:
        peer = await _resolve_peer_robust(client, chat_id)
        m = await client.get_messages(peer, ids=msg_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch message: {exc}")

    if isinstance(m, list):
        m = m[0] if m else None

    if not m or not getattr(m, "media", None):
        raise HTTPException(status_code=404, detail="Media item not found")

    med = m.media
    med_type = type(med).__name__
    if med_type in ("MessageMediaPoll", "MessageMediaContact", "MessageMediaUnsupported"):
        raise HTTPException(status_code=400, detail="Unsupported media type for streaming")

    f = getattr(m, "file", None)
    is_photo = bool(getattr(m, "photo", None) or med_type == "MessageMediaPhoto")
    if f is None and not is_photo:
        raise HTTPException(status_code=404, detail="No downloadable file in message")

    raw_name = getattr(f, "name", None) if f else None
    ext = getattr(f, "ext", "") if f else ""
    if is_photo or (not raw_name and not ext):
        ext = ext or ".jpg"
        filename = raw_name or f"photo_{chat_id}_{msg_id}{ext}"
    else:
        ext = ext or ""
        filename = raw_name or f"file_{chat_id}_{msg_id}{ext}"

    file_size = int(getattr(f, "size", 0) or 0) if f else 0
    mime = getattr(f, "mime_type", None) if f else None
    if not mime or mime == "application/octet-stream":
        mime = "image/jpeg" if is_photo else "application/octet-stream"

    ascii_filename = (
        filename.encode("ascii", "ignore").decode("ascii").replace("\r", "").replace("\n", "").replace('"', "").strip()
        or f"file_{chat_id}_{msg_id}"
    )
    encoded_filename = quote(filename, safe="")

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
        "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; media-src 'self'; img-src 'self'",
    }
    if file_size > 0:
        headers["Content-Length"] = str(file_size)

    async def chunk_generator() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in client.iter_download(m.media, request_size=512 * 1024):
                if chunk:
                    yield chunk
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            return

    return StreamingResponse(chunk_generator(), status_code=200, headers=headers)


class ZipPrepareRequest(BaseModel):
    chat_id: int
    msg_ids: list[int]
    chat_title: str | None = None


@router.post("/api/direct/zip/prepare")
async def prepare_zip_download(req: ZipPrepareRequest, request: Request):
    """Pre-flight validate messages, compute exact archive size, and generate a short-lived download ticket."""
    client = await get_session_client(request, allow_jit=False)
    if not client:
        raise HTTPException(status_code=401, detail="Telegram client not initialized or session not authenticated")

    if not req.msg_ids:
        raise HTTPException(status_code=400, detail="No message IDs provided")

    await _ensure_connected(client)

    try:
        peer = await _resolve_peer_robust(client, req.chat_id)
        messages = await client.get_messages(peer, ids=req.msg_ids)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch messages: {exc}")

    if not isinstance(messages, list):
        messages = [messages] if messages else []

    valid_files: list[dict] = []
    for m in messages:
        if not m or not getattr(m, "media", None):
            continue

        med = m.media
        med_type = type(med).__name__
        if med_type in ("MessageMediaPoll", "MessageMediaContact", "MessageMediaUnsupported"):
            continue

        f = getattr(m, "file", None)
        is_photo = bool(getattr(m, "photo", None) or med_type == "MessageMediaPhoto")
        if f is None and not is_photo:
            continue

        raw_name = getattr(f, "name", None) if f else None
        ext = getattr(f, "ext", "") if f else ""
        mid = int(getattr(m, "id", 0) or 0)
        if is_photo or (not raw_name and not ext):
            ext = ext or ".jpg"
            filename = raw_name or f"photo_{req.chat_id}_{mid}{ext}"
        else:
            ext = ext or ""
            filename = raw_name or f"file_{req.chat_id}_{mid}{ext}"

        file_size = int(getattr(f, "size", 0) or 0) if f else 0
        valid_files.append(
            {
                "msg_id": mid,
                "name": filename,
                "size": file_size,
            }
        )

    if not valid_files:
        raise HTTPException(status_code=404, detail="No downloadable media found in selected messages")

    deduped_files = deduplicate_filenames(valid_files)
    archive_size = calculate_archive_size(deduped_files)

    title = req.chat_title or str(req.chat_id)
    ticket = zip_ticket_manager.create_ticket(
        chat_id=req.chat_id,
        chat_title=title,
        files=deduped_files,
        archive_size=archive_size,
    )

    clean_title = (
        "".join(c for c in title if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
        or f"chat_{req.chat_id}"
    )
    suggested_filename = f"{clean_title}_archive.zip"

    return {
        "ticket": ticket,
        "file_count": len(deduped_files),
        "total_bytes": sum(f["size"] for f in deduped_files),
        "archive_size": archive_size,
        "chat_title": title,
        "suggested_filename": suggested_filename,
    }


@router.get("/api/direct/zip/stream/{ticket}")
async def stream_zip_download(ticket: str, request: Request):
    """Stream on-the-fly ZIP archive using a pre-flight ticket with exact Content-Length."""
    ticket_info = zip_ticket_manager.get_ticket(ticket)
    if not ticket_info:
        raise HTTPException(status_code=404, detail="ZIP download ticket expired or invalid")

    client = await get_session_client(request, allow_jit=False)
    if not client:
        raise HTTPException(status_code=401, detail="Telegram client not initialized or session not authenticated")

    await _ensure_connected(client)

    title = ticket_info.chat_title or str(ticket_info.chat_id)
    clean_title = (
        "".join(c for c in title if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
        or f"chat_{ticket_info.chat_id}"
    )
    filename = f"{clean_title}_archive.zip"

    ascii_filename = (
        filename.encode("ascii", "ignore").decode("ascii").replace("\r", "").replace("\n", "").replace('"', "").strip()
        or f"chat_{ticket_info.chat_id}_archive.zip"
    )
    encoded_filename = quote(filename, safe="")

    headers = {
        "Content-Type": "application/zip",
        "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}",
        "Content-Length": str(ticket_info.estimated_archive_size),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }

    generator = StreamingZipBuilder.stream_archive(
        client=client,
        chat_id=ticket_info.chat_id,
        files=ticket_info.files,
    )
    return StreamingResponse(generator, status_code=200, headers=headers)
