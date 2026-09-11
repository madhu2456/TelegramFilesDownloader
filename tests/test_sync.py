"""Sync-state tests: mocked client only, no network."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.downloader import DownloadOpts, download_chat
from src.store import get_sync_checkpoint, init_db, set_sync_checkpoint


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


def _tgt(cid=9):
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


async def _w(m, file=None):
    Path(str(file)).write_bytes(b"x")


def test_sync_checkpoint_persists(tmp_path):
    conn = init_db(":memory:")
    set_sync_checkpoint(conn, 9, 42)
    assert get_sync_checkpoint(conn, 9) == 42
    assert get_sync_checkpoint(conn, 12345) == 0
    set_sync_checkpoint(conn, 9, 50)
    assert get_sync_checkpoint(conn, 9) == 50
    assert get_sync_checkpoint(conn, 9) > 42


def test_sync_effective_min_id_uses_max_checkpoint(tmp_path):
    conn = init_db(":memory:")
    set_sync_checkpoint(conn, 9, 10)
    c = _cli([_msg(mid=11)], _w)
    r = _run(c, _tgt(9), DownloadOpts(limit=10, sync=True), tmp_path, conn)
    assert c.iter_messages.call_args[1]["min_id"] == 10
    assert r["done"] == 1
    assert get_sync_checkpoint(conn, 9) == 11


def test_sync_min_id_explicit_max_wins(tmp_path):
    conn = init_db(":memory:")
    set_sync_checkpoint(conn, 9, 10)
    # explicit min_id below checkpoint -> checkpoint wins
    c1 = _cli([], None)
    _run(c1, _tgt(9), DownloadOpts(limit=10, sync=True, min_id=5), tmp_path, conn)
    assert c1.iter_messages.call_args[1]["min_id"] == 10
    assert get_sync_checkpoint(conn, 9) == 10
    # explicit min_id above checkpoint -> explicit wins
    c2 = _cli([], None)
    _run(c2, _tgt(9), DownloadOpts(limit=10, sync=True, min_id=20), tmp_path, conn)
    assert c2.iter_messages.call_args[1]["min_id"] == 20
    assert get_sync_checkpoint(conn, 9) == 10


def test_sync_checkpoint_updates_to_max_and_min_id_passthrough(tmp_path):
    conn = init_db(":memory:")
    c = _cli([_msg(mid=11), _msg(mid=12)], _w)
    r = _run(c, _tgt(9), DownloadOpts(limit=10, sync=True), tmp_path, conn)
    assert r["done"] == 2
    assert get_sync_checkpoint(conn, 9) == 12
    # min_id without sync passes straight through
    c2 = _cli([], None)
    _run(c2, _tgt(9), DownloadOpts(limit=10, min_id=7), tmp_path, conn)
    assert c2.iter_messages.call_args[1]["min_id"] == 7
    assert get_sync_checkpoint(conn, 9) == 12
    # no sync and no min_id -> None passed
    c3 = _cli([], None)
    _run(c3, _tgt(9), DownloadOpts(limit=10), tmp_path, conn)
    assert c3.iter_messages.call_args[1]["min_id"] is None
    assert get_sync_checkpoint(conn, 9) == 12


def test_sync_download_failure_does_not_advance_checkpoint(tmp_path):
    conn = init_db(":memory:")
    set_sync_checkpoint(conn, 9, 10)

    async def _dl(m, file=None):
        if m.id == 11:
            Path(str(file)).write_bytes(b"content")
        # mid=12 fails (no part file written)

    c = _cli([_msg(mid=11), _msg(mid=12)], _dl)
    r = _run(c, _tgt(9), DownloadOpts(limit=10, sync=True), tmp_path, conn)
    assert r["done"] == 1
    # Checkpoint advances for successful mid=11, but NOT for failed mid=12
    assert get_sync_checkpoint(conn, 9) == 11
