"""SQLite manifest: expand-only, up-down-up compatible (down=no-op)."""
import json
import sqlite3
from pathlib import Path

_DDL = """CREATE TABLE IF NOT EXISTS downloads(
chat_id INTEGER NOT NULL,msg_id INTEGER NOT NULL,
sha256 TEXT NOT NULL,size INTEGER NOT NULL,relpath TEXT NOT NULL,
created_at TEXT DEFAULT (datetime('now')),
UNIQUE(chat_id,msg_id))"""
_IDX = "CREATE INDEX IF NOT EXISTS idx_downloads_chat ON downloads(chat_id)"
_SYNC_DDL = """CREATE TABLE IF NOT EXISTS sync_state(
chat_id INTEGER PRIMARY KEY,max_msg_id INTEGER NOT NULL)"""
# Expand-only rich metadata columns (keeps UNIQUE(chat_id,msg_id)).
_META_COLS = ["date_utc TEXT", "sender_id INTEGER", "grouped_id INTEGER",
"views INTEGER", "forwards INTEGER", "reactions INTEGER",
"reply_to INTEGER", "snippet TEXT", "mime TEXT"]


def _ensure_meta(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(downloads)").fetchall()}
    for col in _META_COLS:
        if col.split()[0] not in have:
            conn.execute(f"ALTER TABLE downloads ADD COLUMN {col}")


def _conf(c, p):
    c.execute("PRAGMA busy_timeout=5000;")
    c.execute("PRAGMA foreign_keys=ON;")
    if p not in (":memory:", ""):
        try:
            c.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError:
            c.execute("PRAGMA journal_mode=TRUNCATE;")


def init_db(db_path) -> sqlite3.Connection:
    p = str(db_path)
    if p not in (":memory:", ""):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    _conf(c, p)
    c.execute(_DDL)
    c.execute(_IDX)
    c.execute(_SYNC_DDL)
    _ensure_meta(c)
    c.commit()
    return c


def is_downloaded(conn, chat_id, msg_id) -> bool:
    r = conn.execute(
        "SELECT 1 FROM downloads WHERE chat_id=? AND msg_id=? LIMIT 1",
        (chat_id, msg_id)).fetchone()
    return r is not None


def record_download(conn, chat_id, msg_id, sha256, size, relpath, meta=None):
    meta = dict(meta or {})
    cols = ["chat_id", "msg_id", "sha256", "size", "relpath"]
    vals = [chat_id, msg_id, sha256, size, relpath]
    for k in ("date_utc", "sender_id", "grouped_id", "views", "forwards",
              "reactions", "reply_to", "snippet", "mime"):
        if k in meta:
            cols.append(k)
            vals.append(meta[k])
    conn.execute(
        f"INSERT OR IGNORE INTO downloads({','.join(cols)})"
        f" VALUES({','.join('?' * len(vals))})", vals)
    conn.commit()


def find_by_sha(conn, chat_id, sha256):
    return conn.execute(
        "SELECT msg_id,relpath FROM downloads WHERE chat_id=? AND sha256=? LIMIT 1",
        (chat_id, sha256)).fetchone()


def get_sync_checkpoint(conn, chat_id) -> int:
    r = conn.execute(
        "SELECT max_msg_id FROM sync_state WHERE chat_id=? LIMIT 1",
        (chat_id,)).fetchone()
    return int(r[0]) if r else 0


def set_sync_checkpoint(conn, chat_id, max_msg_id):
    conn.execute(
        "INSERT INTO sync_state(chat_id,max_msg_id) VALUES(?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET max_msg_id=max(max_msg_id,excluded.max_msg_id)",
        (chat_id, int(max_msg_id)))
    conn.commit()


def append_jsonl(p, r):
    q = Path(p)
    q.parent.mkdir(parents=True, exist_ok=True)
    with open(q, "a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
