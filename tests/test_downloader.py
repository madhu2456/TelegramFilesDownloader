"""Downloader tests: mocked client only, no network."""
import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telethon.errors import FloodWaitError
from src.downloader import DownloadOpts, download_chat
from src.filesafe import sanitize_component
from src.store import init_db, record_download
def _msg(mid=1, name="f.bin", size=10):
    m = MagicMock(); m.id = mid; m.date = "2024-01-01"; m.sender_id = 1
    m.sender = SimpleNamespace(id=1); m.media = None
    m.file = SimpleNamespace(name=name, size=size)
    return m
async def _agen(items):
    for x in items:
        yield x
def _tgt(cid=1):
    return SimpleNamespace(entity=SimpleNamespace(id=cid))
def _cli(msgs, dl=None):
    c = AsyncMock(); c.iter_messages = MagicMock(return_value=_agen(msgs))
    c.download_media = AsyncMock(side_effect=dl) if dl else AsyncMock()
    return c
def _run(c, tgt, opts, out, conn):
    with patch("src.downloader.asyncio.sleep", new=AsyncMock()), patch("src.downloader._isleep", new=AsyncMock()):
        return asyncio.run(download_chat(c, tgt, opts, out, conn))
def test_bva_limits_0_1_100_101(tmp_path):
    for lim, exp in [(0, 500), (1, 1), (100, 100), (101, 101)]:
        c = _cli([])
        r = _run(c, _tgt(), DownloadOpts(limit=lim), tmp_path, init_db(":memory:"))
        assert c.iter_messages.call_args[1]["limit"] == exp
        assert r["done"] == 0 and r["bytes"] == 0
def test_empty_channel(tmp_path):
    c = _cli([])
    r = _run(c, _tgt(), DownloadOpts(limit=10), tmp_path, init_db(":memory:"))
    assert r == {"done": 0, "skipped": 0, "bytes": 0}
    assert not c.download_media.called
def test_oversize_max_bytes(tmp_path):
    c = _cli([_msg()])
    r = _run(c, _tgt(), DownloadOpts(limit=10, max_bytes=0), tmp_path, init_db(":memory:"))
    assert r["done"] == 0
    assert not c.download_media.called
def test_traversal_and_nul(tmp_path):
    assert sanitize_component("../../etc/passwd") == "passwd"
    assert "/" not in sanitize_component("../../etc/passwd") and ".." not in sanitize_component("../../etc/passwd")
    with pytest.raises(ValueError) as e:
        sanitize_component("a\x00b")
    assert "NUL" in str(e.value)
async def _w(m, file=None):
    Path(str(file)).write_bytes(b"x")
def test_floodwait_resume(tmp_path):
    n = {"c": 0}
    async def _flaky(m, file=None):
        n["c"] += 1
        if n["c"] == 1:
            raise FloodWaitError(None, 1)
        Path(str(file)).write_bytes(b"data")
    c = _cli([_msg()], _flaky)
    r = _run(c, _tgt(), DownloadOpts(limit=10), tmp_path, init_db(":memory:"))
    assert r["done"] == 1
    assert n["c"] == 2
def test_enospc_raises(tmp_path):
    c = _cli([])
    with patch("src.downloader.precheck_disk", side_effect=OSError(errno.ENOSPC, "no space")):
        with pytest.raises(OSError) as e:
            _run(c, _tgt(), DownloadOpts(limit=10), tmp_path, init_db(":memory:"))
    assert e.value.errno == errno.ENOSPC
    assert not c.download_media.called
def test_dry_run_no_write(tmp_path):
    c = _cli([_msg()])
    r = _run(c, _tgt(), DownloadOpts(limit=10, dry_run=True), tmp_path, init_db(":memory:"))
    assert r["done"] == 1
    assert not c.download_media.called
    assert (tmp_path / "messages.jsonl").exists()
def test_resume_unique_skip(tmp_path):
    conn = init_db(":memory:"); record_download(conn, 1, 5, "h", 1, "5_f.bin")
    c = _cli([_msg(mid=5)], _w)
    r = _run(c, _tgt(), DownloadOpts(limit=10, resume=True), tmp_path, conn)
    assert r["skipped"] == 1 and r["done"] == 0
    assert not c.download_media.called
