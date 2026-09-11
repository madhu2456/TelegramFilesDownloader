"""v0.3.1 hardening regression: mocked client only, no network.

Covers 14+/9- diff in src/downloader.py + src/store.py:
- missing-prev fallthrough (no alias_of when dup relpath missing on disk)
- .part cleanup on alias and on skip-same-size
- streamed hash equivalence (chunked 1MiB read == one-shot sha256)
- dry_run never advances sync checkpoint
- atomic max monotonic (set_sync_checkpoint never goes backwards)
- Z-suffix ISO8601 parse to UTC
"""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.downloader import DownloadOpts, _dt, download_chat
from src.filesafe import build_filename, sanitize_component
from src.store import get_sync_checkpoint, init_db, is_downloaded, record_download, set_sync_checkpoint


def _msg(mid=1, name="f.bin", size=10):
    m = MagicMock()
    m.id = mid
    m.date = "2024-01-01"
    m.sender_id = 1
    m.sender = SimpleNamespace(id=1)
    m.media = None
    m.file = SimpleNamespace(name=name, size=size)
    return m


async def _agen(items):
    for x in items:
        yield x


def _tgt(cid=1):
    return SimpleNamespace(entity=SimpleNamespace(id=cid))


def _cli(msgs, dl=None):
    c = AsyncMock()
    c.iter_messages = MagicMock(return_value=_agen(msgs))
    c.download_media = AsyncMock(side_effect=dl) if dl else AsyncMock()
    return c


def _run(c, tgt, opts, out, conn):
    with patch("src.downloader.asyncio.sleep", new=AsyncMock()), patch(
        "src.downloader._isleep", new=AsyncMock()
    ):
        return asyncio.run(download_chat(c, tgt, opts, out, conn))


def test_z_suffix_parse_utc(tmp_path):
    """Z-suffix ISO8601 strings must parse to UTC (v0.3.1 _dt fix)."""
    dt_z = _dt("2024-06-01T12:00:00Z")
    assert dt_z is not None and dt_z.tzinfo == timezone.utc
    assert dt_z.isoformat() == "2024-06-01T12:00:00+00:00"
    # millis variant + equivalence with explicit +00:00
    dt_milli = _dt("2024-06-01T12:00:00.123456Z")
    assert dt_milli is not None and dt_milli.tzinfo == timezone.utc
    assert dt_milli.isoformat() == "2024-06-01T12:00:00.123456+00:00"
    assert dt_z == _dt("2024-06-01T12:00:00+00:00")
    # negative: bogus still fail-closed, None stays None
    assert _dt("bogus") is None
    assert _dt(None) is None
    # integration: --after with Z suffix is accepted (no SystemExit) and filters
    async def _w(m, file=None):
        Path(str(file)).write_bytes(b"x")

    c = _cli([_msg(mid=1)], _w)
    r = _run(c, _tgt(), DownloadOpts(limit=10, after="2024-01-01T00:00:00Z"), tmp_path, init_db(":memory:"))
    assert r["done"] == 1
    assert c.download_media.called
    # boundary: Z-after strictly after message date filters it out
    c2 = _cli([_msg(mid=1)], _w)
    r2 = _run(c2, _tgt(), DownloadOpts(limit=10, after="2024-06-01T00:00:00Z"), tmp_path, init_db(":memory:"))
    assert r2["done"] == 0
    assert not c2.download_media.called


def test_streamed_hash_equivalence_multichunk(tmp_path):
    """Chunked 1MiB hash must equal one-shot sha256 for multi-chunk payloads."""
    # 2 MiB + 123 tail => spans 3 chunk reads (1MiB, 1MiB, 123B); deterministic bytes
    payload = (bytes(range(256)) * 8200)[: (2 * (1 << 20)) + 123]
    assert len(payload) == 2097275
    assert len(payload) > (1 << 20)
    sha_expected = hashlib.sha256(payload).hexdigest()

    async def _dl(m, file=None):
        Path(str(file)).write_bytes(payload)

    c = _cli([_msg(mid=21, name="big.bin", size=len(payload))], _dl)
    conn = init_db(":memory:")
    out = tmp_path / "stream"
    out.mkdir()
    # Guard: new code must never call Path.read_bytes (streamed open+iter);
    # old read_bytes() path would raise here -> proves streaming.
    with patch.object(Path, "read_bytes", side_effect=AssertionError("must stream, not read_bytes")):
        r = _run(c, _tgt(cid=1), DownloadOpts(limit=10), out, conn)
    assert r["done"] == 1
    assert r["skipped"] == 0
    # equivalence: DB + jsonl sha equal one-shot digest
    row = conn.execute("SELECT sha256,size FROM downloads WHERE chat_id=1 AND msg_id=21").fetchone()
    assert row is not None
    assert row[0] == sha_expected
    assert row[1] == len(payload)
    lines = (out / "messages.jsonl").read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["sha"] == sha_expected
    assert "alias_of" not in rec
    # file content equivalence via streamed re-read (no whole-file shortcut assumed)
    dst = out / rec["file"]
    assert dst.exists()
    h2 = hashlib.sha256()
    with open(dst, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h2.update(chunk)
    assert h2.hexdigest() == sha_expected
    assert dst.stat().st_size == len(payload)


def test_missing_prev_fallthrough_no_alias(tmp_path):
    """DB dup whose relpath is missing on disk must fall through (no alias_of)."""
    payload = b"missing-prev-payload"
    sha = hashlib.sha256(payload).hexdigest()
    conn = init_db(":memory:")
    out = tmp_path / "missingprev"
    out.mkdir()
    # ghost row: sha known but file deleted from disk
    record_download(conn, 1, 11, sha, len(payload), "11_deadbeef_ghost.bin")
    assert not (out / "11_deadbeef_ghost.bin").exists()

    async def _dl(m, file=None):
        Path(str(file)).write_bytes(payload)

    c = _cli([_msg(mid=12, name="v.mp4", size=len(payload))], _dl)
    r = _run(c, _tgt(cid=1), DownloadOpts(limit=10), out, conn)
    assert r["done"] == 1
    assert r["skipped"] == 0
    lines = [json.loads(x) for x in (out / "messages.jsonl").read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 1
    # negative: must NOT claim alias_of dead row; must record real file
    assert "alias_of" not in lines[0]
    assert lines[0]["sha"] == sha
    assert lines[0]["id"] == 12
    dst = out / lines[0]["file"]
    assert dst.exists()
    assert dst.stat().st_size == len(payload)
    # fallthrough used normal replace path => no .part litter
    assert list(out.glob("*.part")) == []
    # both rows share sha but distinct msg_ids
    rows = conn.execute("SELECT msg_id,sha256 FROM downloads WHERE chat_id=1 ORDER BY msg_id").fetchall()
    assert [x[0] for x in rows] == [11, 12]
    assert rows[0][1] == rows[1][1] == sha


def test_part_cleanup_on_alias(tmp_path):
    """Dedup alias path must unlink the downloaded .part file."""
    payload = b"alias-cleanup-bytes"
    sha = hashlib.sha256(payload).hexdigest()

    async def _dl(m, file=None):
        Path(str(file)).write_bytes(payload)

    c = _cli(
        [_msg(mid=11, name="v.mp4", size=len(payload)), _msg(mid=12, name="v.mp4", size=len(payload))],
        _dl,
    )
    conn = init_db(":memory:")
    out = tmp_path / "aliasclean"
    out.mkdir()
    r = _run(c, _tgt(cid=1), DownloadOpts(limit=10), out, conn)
    assert r["done"] == 2
    assert r["skipped"] == 0
    lines = [json.loads(x) for x in (out / "messages.jsonl").read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 2
    assert lines[1].get("alias_of") == 11
    assert lines[0]["sha"] == sha and lines[1]["sha"] == sha
    # .part litter must be gone for both messages
    assert list(out.glob("*.part")) == []
    part12 = out / (sanitize_component("1_12_v.mp4") + ".part")
    assert not part12.exists()
    # dst for first message exists; alias file resolvable
    assert (out / lines[0]["file"]).exists()


def test_part_cleanup_on_skip_same_size(tmp_path):
    """Skip-when-dst-same-size path must unlink .part and count skip."""
    payload = b"skip-cleanup-payload-123"
    sha = hashlib.sha256(payload).hexdigest()
    mid, raw = 31, "s.bin"
    dst_name = build_filename(mid, sha, raw)
    out = tmp_path / "skipclean"
    out.mkdir()
    # pre-existing dst with identical size (simulates re-run after crash)
    (out / dst_name).write_bytes(payload)
    assert (out / dst_name).stat().st_size == len(payload)

    async def _dl(m, file=None):
        Path(str(file)).write_bytes(payload)

    c = _cli([_msg(mid=mid, name=raw, size=len(payload))], _dl)
    conn = init_db(":memory:")
    r = _run(c, _tgt(cid=1), DownloadOpts(limit=10), out, conn)
    # same-size dst path: skip counted AND manifest still recorded (v0.3 behavior),
    # but .part must be cleaned (v0.3.1 fix: old code leaked .part here)
    assert r["skipped"] == 1
    assert r["done"] == 0
    assert r["bytes"] == 0
    # .part must be cleaned, dst preserved
    assert list(out.glob("*.part")) == []
    assert (out / dst_name).exists()
    assert (out / dst_name).stat().st_size == len(payload)
    # skip path still records manifest row (v0.3 behavior) with correct sha
    row = conn.execute("SELECT sha256 FROM downloads WHERE chat_id=1 AND msg_id=?", (mid,)).fetchone()
    assert row is not None and row[0] == sha


def test_dry_run_no_checkpoint(tmp_path):
    """sync+dry_run must not advance sync checkpoint and must not write media."""
    conn = init_db(":memory:")
    set_sync_checkpoint(conn, 1, 10)
    assert get_sync_checkpoint(conn, 1) == 10

    async def _dl(m, file=None):  # pragma: no cover - must never be called
        raise AssertionError("dry_run must not call download_media")

    c = _cli([_msg(mid=15)], _dl)
    r = _run(c, _tgt(cid=1), DownloadOpts(limit=10, sync=True, dry_run=True), tmp_path, conn)
    assert r["done"] == 1
    assert not c.download_media.called
    # checkpoint frozen at 10 (old code advanced to 15)
    assert get_sync_checkpoint(conn, 1) == 10
    # dry run records no manifest row
    assert not is_downloaded(conn, 1, 15)
    # jsonl still emitted with dry flag
    lines = (tmp_path / "messages.jsonl").read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["id"] == 15 and rec.get("dry") is True
    # no media files materialized
    assert list(tmp_path.glob("*.part")) == []
    media_files = [p for p in tmp_path.iterdir() if p.is_file() and p.name != "messages.jsonl"]
    assert media_files == []


def test_atomic_max_monotonic(tmp_path):
    """set_sync_checkpoint must be monotonic (never move backwards)."""
    conn = init_db(":memory:")
    assert get_sync_checkpoint(conn, 42) == 0
    set_sync_checkpoint(conn, 42, 50)
    assert get_sync_checkpoint(conn, 42) == 50
    # negative: downgrade attempt must be ignored (old code overwrote to 20)
    set_sync_checkpoint(conn, 42, 20)
    assert get_sync_checkpoint(conn, 42) == 50
    # upgrade still applies
    set_sync_checkpoint(conn, 42, 60)
    assert get_sync_checkpoint(conn, 42) == 60
    # equal + zero-downgrade stay pinned
    set_sync_checkpoint(conn, 42, 60)
    assert get_sync_checkpoint(conn, 42) == 60
    set_sync_checkpoint(conn, 42, 0)
    assert get_sync_checkpoint(conn, 42) == 60
    # isolation: other chats unaffected
    assert get_sync_checkpoint(conn, 999) == 0
    set_sync_checkpoint(conn, 999, 5)
    assert get_sync_checkpoint(conn, 999) == 5
    assert get_sync_checkpoint(conn, 42) == 60
