"""T5 loop: serial gate statvfs jitter resume filters."""
import asyncio
import fnmatch
import hashlib
import logging
import os
import random
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from telethon.errors import FloodWaitError, PeerFloodError
from tqdm import tqdm

try:
    from telethon.errors import TakeoutInitDelayError
except ImportError:
    TakeoutInitDelayError = None
if TYPE_CHECKING:
    from .config import scrub_for_log
    from .filesafe import build_filename, precheck_disk, sanitize_component
    from .store import (
        append_jsonl,
        find_by_sha,
        get_sync_checkpoint,
        is_downloaded,
        record_download,
        set_sync_checkpoint,
    )
else:
    try:
        from .config import scrub_for_log
        from .filesafe import build_filename, precheck_disk, sanitize_component
        from .store import (
            append_jsonl,
            find_by_sha,
            get_sync_checkpoint,
            is_downloaded,
            record_download,
            set_sync_checkpoint,
        )
    except ImportError:
        from config import scrub_for_log
        from filesafe import build_filename, precheck_disk, sanitize_component
        from store import (
            append_jsonl,
            find_by_sha,
            get_sync_checkpoint,
            is_downloaded,
            record_download,
            set_sync_checkpoint,
        )
log = logging.getLogger(__name__); FLOOD_CAP = 300; _stop = False
def _mark(*a):
    global _stop; _stop = True
try: signal.signal(signal.SIGINT, _mark)
except Exception: pass
@dataclass
class DownloadOpts:
    limit: int | None = 500
    max_bytes: int | None = None
    timeout_s: float | None = None
    filter: str | None = None
    after: str | None = None
    before: str | None = None
    from_user: str | None = None
    search: str | None = None
    ids: list | None = None
    dry_run: bool = False
    resume: bool = False
    reverse: bool = True
    takeout: bool = False
    min_id: int | None = None
    max_id: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    pattern: str | None = None
    sync: bool = False
    progress_hook: object = None
    cancel_event: object = None
async def _isleep(s, cancel_event=None):
    e = time.monotonic() + float(s)
    while time.monotonic() < e:
        if _stop or (cancel_event and cancel_event.is_set()): break
        await asyncio.sleep(min(0.5, e - time.monotonic()))
def _dt(s, name=None):
    if s is None: return None
    dt = s if isinstance(s, datetime) else None
    if dt is None:
        try: dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            if name: raise SystemExit(f"invalid --{name} date: {s!r}")
            return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
def _ino(v): return v if isinstance(v, int) and not isinstance(v, bool) else None
def _rxn(m):
    r = getattr(m, "reactions", None)
    if r is None or isinstance(r, int): return _ino(r)
    res = getattr(r, "results", None)
    if isinstance(res, list | tuple):
        try: return sum(int(getattr(x, "count", 0) or 0) for x in res)
        except (TypeError, ValueError): return len(res)
    return _ino(res)
def _meta(m):
    md = _dt(getattr(m, "date", None)); f = getattr(m, "file", None)
    sid = _ino(getattr(m, "sender_id", None) or getattr(getattr(m, "sender", None), "id", None))
    rto = _ino(getattr(m, "reply_to_msg_id", None) or getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None))
    txt = getattr(m, "text", None) or getattr(m, "message", None); txt = txt if isinstance(txt, str) else None
    mime = getattr(f, "mime_type", None) if f is not None else None; mime = mime if isinstance(mime, str) else None
    return {"date_utc": md.isoformat() if md else None, "sender_id": sid, "grouped_id": _ino(getattr(m, "grouped_id", None)), "views": _ino(getattr(m, "views", None)), "forwards": _ino(getattr(m, "forwards", None)), "reactions": _rxn(m), "reply_to": rto, "snippet": txt[:200] if txt else None, "mime": mime}
def _ok(m, o):
    mid = int(getattr(m, "id", 0) or 0)
    if o.max_id is not None and mid >= o.max_id: return False
    sz = int(getattr(getattr(m, "file", None), "size", 0) or 0)
    if o.min_size is not None and sz < o.min_size: return False
    if o.max_size is not None and sz > o.max_size: return False
    if o.pattern:
        f = getattr(m, "file", None)
        if not f:
            return False
        raw_name = getattr(f, "name", None)
        ext = getattr(f, "ext", "") or ""
        candidate = raw_name or f"{mid}{ext}"
        if not fnmatch.fnmatch(candidate.lower(), str(o.pattern).lower()):
            return False
    ad, bd, md = _dt(o.after, "after"), _dt(o.before, "before"), _dt(getattr(m, "date", None))
    if (ad and md and md < ad) or (bd and md and md > bd): return False
    if o.from_user:
        target_sender = str(o.from_user).strip().lower().lstrip("@")
        sid = str(getattr(m, "sender_id", "") or "")
        sender_obj = getattr(m, "sender", None)
        sender_id_str = str(getattr(sender_obj, "id", "") or "")
        s_username = str(getattr(sender_obj, "username", "") or "").lower().lstrip("@")
        s_first = str(getattr(sender_obj, "first_name", "") or "").lower()
        s_last = str(getattr(sender_obj, "last_name", "") or "").lower()
        s_title = str(getattr(sender_obj, "title", "") or "").lower()
        full_name = f"{s_first} {s_last} {s_title}".strip()
        matched = (
            (target_sender in sid and sid != "") or
            (target_sender in sender_id_str and sender_id_str != "") or
            (target_sender == s_username and s_username != "") or
            (target_sender in full_name and full_name != "")
        )
        if not matched: return False
    if o.filter:
        med = getattr(m, "media", None)
        match = bool(getattr(m, o.filter, None) or getattr(med, o.filter, None) or (med and o.filter in str(type(med).__name__).lower()))
        if not match: return False
    return True


def _compute_sha256(file_path: Path) -> str:
    _h = hashlib.sha256()
    with open(file_path, "rb") as _f:
        for _c in iter(lambda: _f.read(1 << 20), b""):
            _h.update(_c)
    return _h.hexdigest()


async def download_chat(client, target, opts, out, conn) -> dict:
    global _stop
    _stop = False
    raw_lim = getattr(opts, "limit", 500)
    lim = None if (raw_lim is None or int(raw_lim) <= 0) else int(raw_lim)
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    ent = getattr(target, "entity", target); cid = int(getattr(ent, "id", 0) or 0)
    _dt(opts.after, "after"); _dt(opts.before, "before")
    log.info("start %s", scrub_for_log({"chat": str(getattr(ent, "id", ent)), "limit": lim})); precheck_disk(out, 1 << 20)
    if opts.sync and opts.ids: log.warning("sync with explicit ids; checkpoint advance may be partial")
    eff = _ino(opts.min_id)
    if opts.sync:
        try: eff = max(int(eff or 0), int(get_sync_checkpoint(conn, cid) or 0)) or None
        except Exception as e: log.warning("sync checkpoint read failed: %s", e)
    eff_min_id = 0 if (eff is None and bool(opts.reverse)) else eff
    active, tcx = client, None
    if getattr(opts, "takeout", False) and hasattr(client, "takeout"):
        try: tcx = client.takeout(); active = await tcx.__aenter__()
        except Exception as e:
            if TakeoutInitDelayError is not None and isinstance(e, TakeoutInitDelayError): log.warning("takeout init delay; falling back"); active, tcx = client, None
            else: raise
    t0, total, done, skip, mx = time.monotonic(), 0, 0, 0, 0; jl = out / "messages.jsonl"
    hook = getattr(opts, "progress_hook", None)
    last_hook_t = 0.0
    cur_stats = [0, 0, 0]

    def _emit_progress(current_file=None, done=None, skip=None, bytes_done=None, relpath=None):
        nonlocal last_hook_t
        if not hook: return
        now = time.monotonic()
        if now - last_hook_t >= 0.25:
            last_hook_t = now
            try:
                eff_done = done if done is not None else cur_stats[0]
                eff_skip = skip if skip is not None else cur_stats[1]
                eff_bytes = bytes_done if bytes_done is not None else cur_stats[2]
                dur = max(0.001, now - t0)
                mbps = (eff_bytes / (1024 * 1024)) / dur
                eta = int((lim - (eff_done + eff_skip)) / max(0.01, (eff_done + eff_skip) / dur)) if (lim and (eff_done + eff_skip) > 0 and lim > (eff_done + eff_skip)) else None
                hook(done=eff_done, skip=eff_skip, total_bytes=eff_bytes, total_msgs=lim, current_file=current_file, speed_mbps=mbps, eta_seconds=eta, relpath=relpath)
            except Exception: pass

    def _make_progress_cb(current_file_name: str, done_count: int, skip_count: int, current_total: int, cancel_ev: object):
        def _progress_cb(current_chunk_bytes: int, chunk_total_bytes: int = 0) -> None:
            if cancel_ev and getattr(cancel_ev, "is_set", lambda: False)():
                raise asyncio.CancelledError("Download cancelled by user")
            _emit_progress(
                current_file=current_file_name,
                done=done_count,
                skip=skip_count,
                bytes_done=current_total + current_chunk_bytes,
            )
        return _progress_cb

    try:
        async with asyncio.Semaphore(1):
            msgs = active.iter_messages(ent, limit=lim, reverse=bool(opts.reverse), search=opts.search, ids=opts.ids, from_user=opts.from_user, min_id=eff_min_id, max_id=opts.max_id); bar = tqdm(total=lim, desc="dl", unit="msg")
            try:
                async for m in msgs:
                    ce = getattr(opts, "cancel_event", None)
                    mid = int(getattr(m, "id", 0) or 0)
                    if bool(opts.reverse) and opts.max_id is not None and mid >= opts.max_id: break
                    if _stop or (ce and ce.is_set()) or (opts.timeout_s and time.monotonic() - t0 > float(opts.timeout_s)) or (opts.max_bytes is not None and total >= int(opts.max_bytes)): break
                    if not _ok(m, opts): bar.update(1); continue
                    mid = int(getattr(m, "id", 0) or 0)
                    if is_downloaded(conn, cid, mid): skip += 1; cur_stats[1] = skip; mx = max(mx, mid); bar.update(1); _emit_progress(); continue
                    raw = getattr(getattr(m, "file", None), "name", None) or "media"
                    part = out / (sanitize_component(f"{cid}_{mid}_{raw}") + ".part")
                    if not opts.resume and part.exists(): part.unlink()
                    sz = int(getattr(getattr(m, "file", None), "size", 0) or 0)
                    if sz: precheck_disk(out, sz)
                    meta = {k: v for k, v in _meta(m).items() if v is not None}
                    if opts.dry_run:
                        append_jsonl(jl, {"id": mid, "dry": True, **meta})
                        done += 1
                        cur_stats[0] = done
                        mx = max(mx, mid)
                        bar.update(1)
                        _emit_progress()
                        continue
                    else:
                        fname = raw
                        dst = None
                        progress_cb = _make_progress_cb(fname, done, skip, total, ce)

                        for _att in range(3):
                            try:
                                try:
                                    await active.download_media(m, file=str(part), progress_callback=progress_cb)
                                except TypeError as te:
                                    if "progress_callback" in str(te):
                                        await active.download_media(m, file=str(part))
                                    else:
                                        raise
                                break
                            except (FloodWaitError, PeerFloodError) as e:
                                if isinstance(e, PeerFloodError) or "PEER_FLOOD" in str(e).upper(): conn.commit(); raise
                                s = int(getattr(e, "seconds", 0) or 0)
                                if s > FLOOD_CAP: conn.commit(); raise
                                ce = getattr(opts, "cancel_event", None)
                                if hook:
                                    try:
                                        hook({"event": "FLOOD_WAIT", "wait_seconds": s, "message": f"Telegram Rate Limited: sleeping {s}s"})
                                    except Exception:
                                        pass
                                await _isleep(s or 1, cancel_event=ce)
                                if _stop or (ce and ce.is_set()):
                                    break
                        else: break
                        if _stop or (ce and ce.is_set()):
                            break
                        if part.exists():
                            h = await asyncio.to_thread(_compute_sha256, part)
                            dst = out / build_filename(mid, h, raw); dup = find_by_sha(conn, cid, h)
                            if dup and int(dup[0]) != mid and (out / str(dup[1])).exists():
                                prev = out / str(dup[1])
                                try:
                                    if not dst.exists(): os.link(str(prev), str(dst))
                                except OSError: pass
                                fn2 = dst.name if dst.exists() else str(dup[1]); sz2 = (dst.stat().st_size if dst.exists() else 0) or (prev.stat().st_size if prev.exists() else 0)
                                part.unlink(missing_ok=True)
                                record_download(conn, cid, mid, h, sz2, fn2, meta); append_jsonl(jl, {"id": mid, "file": fn2, "sha": h, "alias_of": int(dup[0]), **meta}); done += 1; cur_stats[0] = done; mx = max(mx, mid)
                                total += sz2
                                cur_stats[2] = total
                            else:
                                if dst.exists() and dst.stat().st_size == part.stat().st_size:
                                    part.unlink(missing_ok=True)
                                    skip += 1
                                    cur_stats[1] = skip
                                    record_download(conn, cid, mid, h, dst.stat().st_size, dst.name, meta=_meta(m))
                                    append_jsonl(jl, {"id": mid, "skipped": True, "path": dst.name, **_meta(m)})
                                    mx = max(mx, mid)
                                else:
                                    os.replace(str(part), str(dst))
                                    os.chmod(str(dst), 0o600)
                                    try:
                                        pfd = os.open(str(dst.parent), os.O_RDONLY)
                                        os.fsync(pfd)
                                        os.close(pfd)
                                    except OSError:
                                        pass
                                    record_download(conn, cid, mid, h, dst.stat().st_size if dst.exists() else 0, dst.name, meta)
                                    append_jsonl(jl, {"id": mid, "file": dst.name, "sha": h, **meta})
                                    total += dst.stat().st_size if dst.exists() else 0
                                    cur_stats[2] = total
                                    done += 1
                                    cur_stats[0] = done
                                    mx = max(mx, mid)
                    bar.update(1); _emit_progress(raw, relpath=str(dst) if dst is not None else None); await asyncio.sleep(1.0 + random.random() * 0.5)
            except (FloodWaitError, PeerFloodError) as e:
                s = int(getattr(e, "seconds", 0) or 0)
                if isinstance(e, PeerFloodError) or s > FLOOD_CAP or "PEER_FLOOD" in type(e).__name__.upper() or "PEER_FLOOD" in str(e).upper(): conn.commit(); raise
                ce = getattr(opts, "cancel_event", None)
                if hook:
                    try:
                        hook({"event": "FLOOD_WAIT", "wait_seconds": s, "message": f"Telegram Rate Limited: sleeping {s}s"})
                    except Exception:
                        pass
                await _isleep(min(s or 1, FLOOD_CAP), cancel_event=ce)
            finally: bar.close()
    finally:
        if hook:
            try:
                dur = max(0.001, time.monotonic() - t0)
                mbps = (total / (1024 * 1024)) / dur if done > 0 else 0.0
                hook(done=done, skip=skip, total_bytes=total, total_msgs=lim, current_file=None, speed_mbps=mbps, eta_seconds=0)
            except Exception: pass
        if tcx is not None:
            try: await tcx.__aexit__(None, None, None)
            except Exception: pass
    if opts.sync and mx and not opts.dry_run:
        try:
            if mx > int(get_sync_checkpoint(conn, cid) or 0): set_sync_checkpoint(conn, cid, mx)
        except Exception as e: log.warning("sync checkpoint write failed: %s", e)
    return {"done": done, "skipped": skip, "bytes": total}
