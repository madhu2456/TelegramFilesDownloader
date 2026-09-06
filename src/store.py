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
    c.commit()
    return c


def is_downloaded(conn, chat_id, msg_id) -> bool:
    r = conn.execute(
        "SELECT 1 FROM downloads WHERE chat_id=? AND msg_id=? LIMIT 1",
        (chat_id, msg_id)).fetchone()
    return r is not None


def record_download(conn, chat_id, msg_id, sha256, size, relpath):
    conn.execute(
        "INSERT OR IGNORE INTO downloads(chat_id,msg_id,sha256,size,relpath)"
        " VALUES(?,?,?,?,?)", (chat_id, msg_id, sha256, size, relpath))
    conn.commit()


def append_jsonl(p, r):
    q = Path(p)
    q.parent.mkdir(parents=True, exist_ok=True)
    with open(q, "a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
