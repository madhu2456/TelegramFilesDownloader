"""Media gallery pagination, byte-range HTTP 206 streaming, and disk monitor."""
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.store import _ensure_meta

router = APIRouter()


def _connect_manifest(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def classify_mime(mime: str | None, relpath: str) -> str:
    m = (mime or "").lower()
    fn = relpath.lower()
    if m.startswith("video/") or fn.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v")):
        return "video"
    if m.startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg")):
        return "photo"
    if m.startswith("audio/") or fn.endswith((".mp3", ".m4a", ".ogg", ".flac", ".wav", ".opus")):
        return "audio"
    return "document"


def parse_range_header(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if file_size == 0:
        return None
    if not range_header:
        return (0, file_size - 1)
    if not range_header.startswith("bytes="):
        return None
    rng = range_header[6:].strip()
    parts = rng.split("-", 1)
    if len(parts) != 2:
        return None
    s_str, e_str = parts[0].strip(), parts[1].strip()
    try:
        if s_str == "":
            length = int(e_str) if e_str.isdigit() else 0
            start = max(0, file_size - length)
            end = file_size - 1
        elif e_str == "":
            start = int(s_str)
            end = file_size - 1
        else:
            start = int(s_str)
            end = int(e_str)
    except ValueError:
        return None

    if file_size == 0:
        return None

    if start >= file_size or start > end or start < 0:
        raise HTTPException(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
            detail="Range Not Satisfiable",
        )
    end = min(end, file_size - 1)
    return (start, end)


@router.get("/api/media")
async def get_media(request: Request, page: int = 1, limit: int = 50, kind: str | None = None):
    out_dir = Path(getattr(request.app.state, "out_dir", "out")).resolve()
    db_path = out_dir / "manifest.db"
    limit = min(max(1, limit), 100)
    page = max(1, page)
    offset = (page - 1) * limit

    if not db_path.exists():
        return {"items": [], "pagination": {"page": page, "limit": limit, "total": 0, "pages": 1}}

    conn = _connect_manifest(db_path)
    try:
        try:
            _ensure_meta(conn)
        except sqlite3.OperationalError:
            pass

        have_cols = {r[1] for r in conn.execute("PRAGMA table_info(downloads)").fetchall()}
        date_utc_expr = "date_utc" if "date_utc" in have_cols else "NULL AS date_utc"
        mime_expr = "mime" if "mime" in have_cols else "NULL AS mime"
        snippet_expr = "snippet" if "snippet" in have_cols else "NULL AS snippet"

        where_clauses = []
        if kind and kind != "all":
            k_lower = kind.lower()
            video_cond = "(LOWER(COALESCE(mime, '')) LIKE 'video/%' OR LOWER(relpath) LIKE '%.mp4' OR LOWER(relpath) LIKE '%.mkv' OR LOWER(relpath) LIKE '%.avi' OR LOWER(relpath) LIKE '%.mov' OR LOWER(relpath) LIKE '%.webm' OR LOWER(relpath) LIKE '%.flv' OR LOWER(relpath) LIKE '%.m4v')"
            photo_cond = "(LOWER(COALESCE(mime, '')) LIKE 'image/%' OR LOWER(relpath) LIKE '%.jpg' OR LOWER(relpath) LIKE '%.jpeg' OR LOWER(relpath) LIKE '%.png' OR LOWER(relpath) LIKE '%.webp' OR LOWER(relpath) LIKE '%.gif' OR LOWER(relpath) LIKE '%.bmp' OR LOWER(relpath) LIKE '%.svg')"
            audio_cond = "(LOWER(COALESCE(mime, '')) LIKE 'audio/%' OR LOWER(relpath) LIKE '%.mp3' OR LOWER(relpath) LIKE '%.m4a' OR LOWER(relpath) LIKE '%.ogg' OR LOWER(relpath) LIKE '%.flac' OR LOWER(relpath) LIKE '%.wav' OR LOWER(relpath) LIKE '%.opus')"
            if k_lower == "video":
                where_clauses.append(video_cond)
            elif k_lower in ("photo", "image"):
                where_clauses.append(photo_cond)
            elif k_lower == "audio":
                where_clauses.append(audio_cond)
            elif k_lower in ("document", "doc"):
                where_clauses.append(f"(NOT ({video_cond} OR {photo_cond} OR {audio_cond}))")

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        cur = conn.cursor()
        total = cur.execute(f"SELECT count(*) FROM downloads{where_sql}").fetchone()[0]
        rows = cur.execute(
            f"SELECT chat_id, msg_id, sha256, size, relpath, created_at, {date_utc_expr}, {mime_expr}, {snippet_expr} FROM downloads{where_sql} ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

        items = []
        for r in rows:
            relpath = r["relpath"]
            filename = Path(relpath).name
            k = classify_mime(r["mime"], relpath)
            target = (out_dir / relpath).resolve()
            exists = target.is_relative_to(out_dir) and target.exists()
            items.append({
                "chat_id": r["chat_id"],
                "msg_id": r["msg_id"],
                "sha256": r["sha256"],
                "size": r["size"],
                "filename": filename,
                "relpath": relpath,
                "date_utc": r["date_utc"],
                "mime": r["mime"],
                "snippet": r["snippet"],
                "kind": k,
                "exists": exists,
                "stream_url": f"/api/media/stream/{r['chat_id']}/{r['msg_id']}",
            })
        pages = (total + limit - 1) // limit if total > 0 else 1
        return {
            "items": items,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": pages,
            },
        }
    finally:
        conn.close()


@router.get("/api/media/stream/{chat_id}/{msg_id}")
async def stream_media(request: Request, chat_id: int, msg_id: int):
    out_dir = Path(getattr(request.app.state, "out_dir", "out")).resolve()
    db_path = out_dir / "manifest.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Manifest database not found")

    conn = _connect_manifest(db_path)
    try:
        have_cols = {r[1] for r in conn.execute("PRAGMA table_info(downloads)").fetchall()}
        mime_expr = "mime" if "mime" in have_cols else "NULL AS mime"
        row = conn.cursor().execute(
            f"SELECT relpath, {mime_expr} FROM downloads WHERE chat_id = ? AND msg_id = ?",
            (chat_id, msg_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Media item not found in manifest")

    target_file = (out_dir / row["relpath"]).resolve()
    if not target_file.is_relative_to(out_dir):
        raise HTTPException(status_code=403, detail="Forbidden: path outside output directory")
    if not target_file.is_file():
        raise HTTPException(status_code=404, detail="Media file not found on disk")

    file_size = target_file.stat().st_size
    range_header = request.headers.get("range")
    parsed_range = parse_range_header(range_header, file_size)

    disposition_type = "attachment" if request.query_params.get("download") == "1" else "inline"
    filename = Path(row["relpath"]).name
    ascii_filename = filename.encode("ascii", "ignore").decode("ascii").replace("\r", "").replace("\n", "").replace('"', "").strip() or "file"
    encoded_filename = quote(filename, safe="")

    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": row["mime"] or "application/octet-stream",
        "Content-Disposition": f'{disposition_type}; filename="{ascii_filename}"; filename*=UTF-8\'\'{encoded_filename}',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; media-src 'self'; img-src 'self'",
    }

    if file_size == 0:
        common_headers["Content-Length"] = "0"
        return StreamingResponse(iter([]), status_code=200, headers=common_headers)

    if not range_header or parsed_range is None:
        def iter_full():
            with open(target_file, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk

        common_headers["Content-Length"] = str(file_size)
        return StreamingResponse(iter_full(), status_code=200, headers=common_headers)

    start, end = parsed_range
    chunk_size = end - start + 1

    def iterfile():
        with open(target_file, "rb") as f:
            f.seek(start)
            remaining = chunk_size
            while remaining > 0:
                chunk = f.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    common_headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    common_headers["Content-Length"] = str(chunk_size)
    return StreamingResponse(iterfile(), status_code=206, headers=common_headers)


@router.get("/api/system/storage")
async def get_storage(request: Request):
    out_dir = Path(getattr(request.app.state, "out_dir", "out")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    vfs = os.statvfs(str(out_dir))
    total_bytes = vfs.f_frsize * vfs.f_blocks
    free_bytes = vfs.f_frsize * vfs.f_bavail
    used_bytes = total_bytes - free_bytes
    used_percent = round((used_bytes / total_bytes) * 100, 1) if total_bytes > 0 else 0.0
    is_low_space = (free_bytes / total_bytes) < 0.10 if total_bytes > 0 else False
    return {
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "used_bytes": used_bytes,
        "used_percent": used_percent,
        "is_low_space": is_low_space,
    }
