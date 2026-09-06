"""T5 CLI: build_parser 0o600 load_config->get_client->resolve_target->download_chat."""
import argparse, asyncio, logging, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from telethon import TelegramClient
from telethon.errors import FloodWaitError
try:
    from .config import ensure_out_dir, load_config
    from .resolver import ResolveError, resolve_target
    from .store import init_db
    from .downloader import DownloadOpts, download_chat
except ImportError:
    from config import ensure_out_dir, load_config
    from resolver import ResolveError, resolve_target
    from store import init_db
    from downloader import DownloadOpts, download_chat
log = logging.getLogger(__name__)
def _chmod600_tree(out):
    r = Path(out); r.mkdir(parents=True, exist_ok=True)
    for p in r.rglob("*"):
        try: os.chmod(str(p), 0o600 if p.is_file() else 0o700)
        except OSError: pass
    try: os.chmod(str(r), 0o700)
    except OSError: pass
    return r
def get_client(cfg):
    try: from .auth import ensure_session_file
    except ImportError: from auth import ensure_session_file
    ensure_session_file(cfg.session_path)
    return TelegramClient(str(cfg.session_path), int(cfg.api_id), str(cfg.api_hash))
def build_parser():
    p = argparse.ArgumentParser(prog="tg-dl", description="Telegram downloader target chat media")
    p.add_argument("--target", required=True, help="target chat @user t.me link id phone"); p.add_argument("--out", default="out", help="output dir")
    p.add_argument("--limit", type=int, default=500, help="bounded limit max 500"); p.add_argument("--filter", default=None, help="media type filter")
    p.add_argument("--after", default=None, help="after date"); p.add_argument("--before", default=None, help="before date")
    p.add_argument("--from-user", default=None, help="sender filter"); p.add_argument("--search", default=None, help="search text")
    p.add_argument("--ids", default=None, help="comma ids"); p.add_argument("--max-bytes", type=int, default=None); p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--dry-run", action="store_true", help="no bytes"); p.add_argument("--resume", action="store_true", help="keep .part resume")
    p.add_argument("--join", action="store_true", default=False, help="join default false"); p.add_argument("--takeout", action="store_true", default=False, help="takeout opt-in")
    p.add_argument("--verbose", action="store_true"); return p
def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO)
    try: cfg = load_config()
    except SystemExit: return 1
    except Exception: return 1
    if bool(getattr(a, "takeout", False)): cfg.takeout = True
    try: out = ensure_out_dir(a.out); _chmod600_tree(out)
    except (OSError, SystemExit): return 4
    ids = [int(x) for x in str(a.ids).split(",") if x.strip().isdigit()] if a.ids else None
    opts = DownloadOpts(limit=min(int(a.limit or 500), 500), max_bytes=a.max_bytes, timeout_s=a.timeout, filter=a.filter, after=a.after, before=a.before, from_user=getattr(a, "from_user", None), search=a.search, ids=ids, dry_run=bool(a.dry_run), resume=bool(a.resume))
    async def _run():
        c = get_client(cfg)
        async with c:
            try: t = await resolve_target(c, str(a.target), join=bool(a.join))
            except ResolveError as e: return 5 if getattr(e, "code", "") == "EXIT5" else 5
            db = init_db(Path(out) / "manifest.db")
            try: return await download_chat(c, t, opts, out, db)
            except FloodWaitError: return 3
        return 0
    try: r = asyncio.run(_run())
    except FloodWaitError: return 3
    except (OSError, IOError): return 4
    except SystemExit as e: return int(e.code) if str(getattr(e, "code", "")).isdigit() else 1
    except Exception as e:
        if "auth" in str(type(e).__name__).lower() or "auth" in str(e).lower(): return 2
        return 1
    if isinstance(r, dict): return 0
    return int(r or 0) if isinstance(r, int) else 0
if __name__ == "__main__": sys.exit(main())
