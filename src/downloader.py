"""T5 loop: serial gate statvfs jitter resume filters."""
import asyncio, hashlib, logging, os, random, signal, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tqdm import tqdm
from telethon.errors import FloodWaitError
try: from telethon.errors import TakeoutInitDelayError
except ImportError: TakeoutInitDelayError = None
try:
    from .config import scrub_for_log
    from .filesafe import build_filename, precheck_disk, sanitize_component
    from .store import append_jsonl, find_by_sha, get_sync_checkpoint, is_downloaded, record_download, set_sync_checkpoint
except ImportError:
    from config import scrub_for_log
    from filesafe import build_filename, precheck_disk, sanitize_component
    from store import append_jsonl, find_by_sha, get_sync_checkpoint, is_downloaded, record_download, set_sync_checkpoint
log = logging.getLogger(__name__); FLOOD_CAP = 300; _stop = False
def _mark(*a):
    global _stop; _stop = True
try: signal.signal(signal.SIGINT, _mark)
except Exception: pass
@dataclass
class DownloadOpts:
    limit: int = 500
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
    sync: bool = False
async def _isleep(s):
    e = time.monotonic() + float(s)
    while time.monotonic() < e:
        if _stop: break
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
    if isinstance(res, (list, tuple)):
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
    ad, bd, md = _dt(o.after, "after"), _dt(o.before, "before"), _dt(getattr(m, "date", None))
    if (ad and md and md < ad) or (bd and md and md > bd): return False
    u = str(getattr(m, "sender_id", "") or "") + str(getattr(getattr(m, "sender", None), "id", "") or "")
    return not ((o.from_user and str(o.from_user) not in u) or (o.filter and o.filter not in str(type(getattr(m, "media", None)).__name__).lower() and not getattr(getattr(m, "media", None), o.filter, None)))
async def download_chat(client, target, opts, out, conn) -> dict:
    lim = min(int(opts.limit or 500), 500); out = Path(out); out.mkdir(parents=True, exist_ok=True)
    ent = getattr(target, "entity", target); cid = int(getattr(ent, "id", 0) or 0)
    _dt(opts.after, "after"); _dt(opts.before, "before")
    log.info("start %s", scrub_for_log({"chat": str(getattr(ent, "id", ent)), "limit": lim})); precheck_disk(out, 1 << 20)
    if opts.sync and opts.ids: log.warning("sync with explicit ids; checkpoint advance may be partial")
    eff = _ino(opts.min_id)
    if opts.sync:
        try: eff = max(int(eff or 0), int(get_sync_checkpoint(conn, cid) or 0)) or None
        except Exception as e: log.warning("sync checkpoint read failed: %s", e)
    active, tcx = client, None
    if getattr(opts, "takeout", False) and hasattr(client, "takeout"):
        try: tcx = client.takeout(); active = await tcx.__aenter__()
        except Exception as e:
            if TakeoutInitDelayError is not None and isinstance(e, TakeoutInitDelayError): log.warning("takeout init delay; falling back"); active, tcx = client, None
            else: raise
    t0, total, done, skip, mx = time.monotonic(), 0, 0, 0, 0; jl = out / "messages.jsonl"
    try:
        async with asyncio.Semaphore(1):
            msgs = active.iter_messages(ent, limit=lim, reverse=bool(opts.reverse), search=opts.search, ids=opts.ids, from_user=opts.from_user, min_id=eff); bar = tqdm(total=lim, desc="dl", unit="msg")
            try:
                async for m in msgs:
                    if _stop or (opts.timeout_s and time.monotonic() - t0 > float(opts.timeout_s)) or (opts.max_bytes is not None and total >= int(opts.max_bytes)): break
                    if not _ok(m, opts): bar.update(1); continue
                    mid = int(getattr(m, "id", 0) or 0); mx = max(mx, mid)
                    if is_downloaded(conn, cid, mid): skip += 1; bar.update(1); continue
                    raw = getattr(getattr(m, "file", None), "name", None) or "media"
                    part = out / (sanitize_component(f"{mid}_{raw}") + ".part")
                    if not opts.resume and part.exists(): part.unlink()
                    sz = int(getattr(getattr(m, "file", None), "size", 0) or 0)
                    if sz: precheck_disk(out, sz)
                    meta = {k: v for k, v in _meta(m).items() if v is not None}
                    if opts.dry_run: append_jsonl(jl, {"id": mid, "dry": True, **meta}); done += 1; bar.update(1)
                    else:
                        for att in range(3):
                            try: await active.download_media(m, file=str(part)); break
                            except FloodWaitError as e:
                                s = int(getattr(e, "seconds", 0) or 0)
                                if s > FLOOD_CAP or "PEER_FLOOD" in str(e).upper(): conn.commit(); raise
                                await _isleep(s or 1)
                        else: break
                        if part.exists():
                            _h = hashlib.sha256()
                            with open(part, "rb") as _f:
                                for _c in iter(lambda: _f.read(1 << 20), b""): _h.update(_c)
                            h = _h.hexdigest(); dst = out / build_filename(mid, h, raw); dup = find_by_sha(conn, cid, h)
                            if dup and int(dup[0]) != mid and (out / str(dup[1])).exists():
                                prev = out / str(dup[1])
                                try:
                                    if not dst.exists(): os.link(str(prev), str(dst))
                                except OSError: pass
                                fn2 = dst.name if dst.exists() else str(dup[1]); sz2 = (dst.stat().st_size if dst.exists() else 0) or (prev.stat().st_size if prev.exists() else 0)
                                part.unlink(missing_ok=True)
                                record_download(conn, cid, mid, h, sz2, fn2, meta); append_jsonl(jl, {"id": mid, "file": fn2, "sha": h, "alias_of": int(dup[0]), **meta}); done += 1
                            else:
                                if dst.exists() and dst.stat().st_size == part.stat().st_size: part.unlink(missing_ok=True); skip += 1
                                else: os.replace(str(part), str(dst)); os.chmod(str(dst), 0o600)
                                record_download(conn, cid, mid, h, dst.stat().st_size if dst.exists() else 0, dst.name, meta); append_jsonl(jl, {"id": mid, "file": dst.name, "sha": h, **meta}); total += dst.stat().st_size if dst.exists() else 0; done += 1
                    bar.update(1); await asyncio.sleep(1.0 + random.random() * 0.5)
            except FloodWaitError as e:
                s = int(getattr(e, "seconds", 0) or 0)
                if s > FLOOD_CAP or "PEER_FLOOD" in type(e).__name__.upper() or "PEER_FLOOD" in str(e).upper(): conn.commit(); raise
                await _isleep(min(s or 1, FLOOD_CAP))
            finally: bar.close()
    finally:
        if tcx is not None:
            try: await tcx.__aexit__(None, None, None)
            except Exception: pass
    if opts.sync and mx and not opts.dry_run:
        try:
            if mx > int(get_sync_checkpoint(conn, cid) or 0): set_sync_checkpoint(conn, cid, mx)
        except Exception as e: log.warning("sync checkpoint write failed: %s", e)
    return {"done": done, "skipped": skip, "bytes": total}
