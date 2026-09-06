"""T5 loop: serial gate statvfs jitter resume filters."""
import asyncio, hashlib, logging, os, random, signal, time
from dataclasses import dataclass
from pathlib import Path
from tqdm import tqdm
from telethon.errors import FloodWaitError
try:
    from .config import scrub_for_log
    from .filesafe import precheck_disk, sanitize_component
    from .store import append_jsonl, is_downloaded, record_download
except ImportError:
    from config import scrub_for_log
    from filesafe import precheck_disk, sanitize_component
    from store import append_jsonl, is_downloaded, record_download
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
async def _isleep(s):
    e = time.monotonic() + float(s)
    while time.monotonic() < e:
        if _stop: break
        await asyncio.sleep(min(0.5, e - time.monotonic()))
def _ok(m, o):
    d = str(getattr(m, "date", "") or ""); u = str(getattr(m, "sender_id", "") or "") + str(getattr(getattr(m, "sender", None), "id", "") or "")
    return not ((o.after and d < str(o.after)) or (o.before and d > str(o.before)) or (o.from_user and str(o.from_user) not in u) or (o.filter and o.filter not in str(type(getattr(m, "media", None)).__name__).lower() and not getattr(getattr(m, "media", None), o.filter, None)))
async def download_chat(client, target, opts, out, conn) -> dict:
    lim = min(int(opts.limit or 500), 500); out = Path(out); out.mkdir(parents=True, exist_ok=True)
    ent = getattr(target, "entity", target); cid = int(getattr(ent, "id", 0) or 0)
    log.info("start %s", scrub_for_log({"chat": str(getattr(ent, "id", ent)), "limit": lim})); precheck_disk(out, 1 << 20)
    t0, total, done, skip = time.monotonic(), 0, 0, 0; jl = out / "messages.jsonl"
    async with asyncio.Semaphore(1):
        msgs = client.iter_messages(ent, limit=lim, reverse=bool(opts.reverse), search=opts.search, ids=opts.ids, from_user=opts.from_user)
        bar = tqdm(total=lim, desc="dl", unit="msg")
        try:
            async for m in msgs:
                if _stop or (opts.timeout_s and time.monotonic() - t0 > float(opts.timeout_s)) or (opts.max_bytes is not None and total >= int(opts.max_bytes)): break
                if not _ok(m, opts): bar.update(1); continue
                mid = int(getattr(m, "id", 0) or 0)
                if is_downloaded(conn, cid, mid): skip += 1; bar.update(1); continue
                fn = sanitize_component(f"{mid}_{getattr(getattr(m, 'file', None), 'name', None) or 'media'}"); dst = out / fn; part = out / (fn + ".part")
                offset = part.stat().st_size if (opts.resume and part.exists()) else 0
                sz = int(getattr(getattr(m, "file", None), "size", 0) or 0)
                if sz: precheck_disk(out, max(0, sz - offset))
                if opts.dry_run: append_jsonl(jl, {"id": mid, "dry": True}); done += 1; bar.update(1)
                else:
                    for att in range(3):
                        try: await client.download_media(m, file=str(part)); break
                        except FloodWaitError as e:
                            s = int(getattr(e, "seconds", 0) or 0)
                            if s > FLOOD_CAP or "PEER_FLOOD" in str(e).upper(): conn.commit(); raise
                            await _isleep(s or 1)
                    else: break
                    if part.exists():
                        h = hashlib.sha256(part.read_bytes()).hexdigest()
                        if dst.exists() and dst.stat().st_size == part.stat().st_size: skip += 1
                        else: os.replace(str(part), str(dst)); os.chmod(str(dst), 0o600)
                        record_download(conn, cid, mid, h, dst.stat().st_size if dst.exists() else 0, dst.name); append_jsonl(jl, {"id": mid, "file": dst.name, "sha": h})
                        total += dst.stat().st_size if dst.exists() else 0; done += 1
                bar.update(1); await asyncio.sleep(1.0 + random.random() * 0.5)
        except FloodWaitError as e:
            s = int(getattr(e, "seconds", 0) or 0)
            if s > FLOOD_CAP or "PEER_FLOOD" in type(e).__name__.upper() or "PEER_FLOOD" in str(e).upper(): conn.commit(); raise
            await _isleep(min(s or 1, FLOOD_CAP))
        finally: bar.close()
    return {"done": done, "skipped": skip, "bytes": total}
