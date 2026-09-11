"""Downloader tests: mocked client only, no network."""
import asyncio
import errno
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telethon.errors import FloodWaitError, TakeoutInitDelayError
from src.downloader import DownloadOpts, _dt, _ok, download_chat
from src.filesafe import build_filename, sanitize_component
from src.store import get_sync_checkpoint, init_db, record_download, set_sync_checkpoint
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
def test_resume_part_delete_vs_keep(tmp_path):
    out_noresume = tmp_path / "noresume"; out_noresume.mkdir()
    part_nr = out_noresume / (sanitize_component("1_1_f.bin") + ".part")
    part_nr.write_bytes(b"stale")
    seen_nr = {}
    async def _dl_nr(m, file=None):
        seen_nr["exists_at_dl"] = Path(str(file)).exists()
        Path(str(file)).write_bytes(b"new-bytes")
    c_nr = _cli([_msg(mid=1)], _dl_nr)
    r_nr = _run(c_nr, _tgt(), DownloadOpts(limit=10, resume=False), out_noresume, init_db(":memory:"))
    assert seen_nr["exists_at_dl"] is False
    assert r_nr["done"] == 1
    assert not part_nr.exists()
    out_resume = tmp_path / "resume"; out_resume.mkdir()
    part_r = out_resume / (sanitize_component("1_1_f.bin") + ".part")
    part_r.write_bytes(b"stale")
    seen_r = {}
    async def _dl_r(m, file=None):
        seen_r["exists_at_dl"] = Path(str(file)).exists()
        Path(str(file)).write_bytes(b"new-bytes")
    c_r = _cli([_msg(mid=1)], _dl_r)
    r_r = _run(c_r, _tgt(), DownloadOpts(limit=10, resume=True), out_resume, init_db(":memory:"))
    assert seen_r["exists_at_dl"] is True
    assert r_r["done"] == 1
def test_takeout_fallback_on_init_delay(tmp_path):
    tcx = AsyncMock()
    tcx.__aenter__ = AsyncMock(side_effect=TakeoutInitDelayError(None, 1))
    tcx.__aexit__ = AsyncMock(return_value=None)
    c = _cli([_msg(mid=1)], _w)
    c.takeout = MagicMock(return_value=tcx)
    conn = init_db(":memory:")
    r = _run(c, _tgt(), DownloadOpts(limit=10, takeout=True), tmp_path, conn)
    assert r["done"] == 1
    assert c.download_media.called
    assert c.iter_messages.called
    assert tcx.__aenter__.called
def test_takeout_wrapper_uses_takeout_client(tmp_path):
    takeout_active = AsyncMock()
    takeout_active.iter_messages = MagicMock(return_value=_agen([_msg(mid=2)]))
    takeout_active.download_media = AsyncMock(side_effect=_w)
    tcx = AsyncMock()
    tcx.__aenter__ = AsyncMock(return_value=takeout_active)
    tcx.__aexit__ = AsyncMock(return_value=None)
    base = AsyncMock()
    base.iter_messages = MagicMock(return_value=_agen([]))
    base.download_media = AsyncMock()
    base.takeout = MagicMock(return_value=tcx)
    r = _run(base, _tgt(), DownloadOpts(limit=10, takeout=True), tmp_path, init_db(":memory:"))
    assert r["done"] == 1
    assert takeout_active.iter_messages.called
    assert takeout_active.download_media.called
    assert not base.iter_messages.called
    assert not base.download_media.called
    assert tcx.__aexit__.called
def test_utc_valid_normalizes(tmp_path):
    from datetime import timezone
    dt = _dt("2024-06-01T12:00:00+02:00")
    assert dt is not None and dt.tzinfo == timezone.utc
    assert (dt.hour, dt.minute) == (10, 0)
    assert dt.isoformat() == "2024-06-01T10:00:00+00:00"
    naive = _dt("2024-01-01")
    assert naive is not None and naive.tzinfo == timezone.utc
    assert naive.isoformat() == "2024-01-01T00:00:00+00:00"
def test_utc_invalid_fail_closed(tmp_path):
    assert _dt("bogus") is None
    assert _dt(None) is None
    with pytest.raises(SystemExit, match="invalid --after"):
        _dt("bogus", "after")
    c = _cli([_msg()])
    with pytest.raises(SystemExit, match="invalid --after"):
        _run(c, _tgt(), DownloadOpts(limit=10, after="not-a-date"), tmp_path, init_db(":memory:"))
    assert not c.download_media.called
def test_rich_metadata_columns_present(tmp_path):
    m = MagicMock(); m.id = 7; m.date = "2024-01-01T10:00:00+00:00"
    m.sender_id = 7; m.sender = SimpleNamespace(id=7); m.media = None
    m.grouped_id = 99; m.views = 5; m.forwards = 2; m.reactions = 4
    m.reply_to_msg_id = 3; m.text = "hello world"; m.message = None
    m.file = SimpleNamespace(name="a.mp4", size=3, mime_type="video/mp4")
    async def _dl(m, file=None):
        Path(str(file)).write_bytes(b"xyz")
    c = _cli([m], _dl)
    conn = init_db(":memory:")
    out = tmp_path / "rich"; out.mkdir()
    r = _run(c, _tgt(cid=1), DownloadOpts(limit=10), out, conn)
    assert r["done"] == 1
    row = conn.execute("SELECT date_utc,sender_id,grouped_id,views,forwards,reactions,reply_to,snippet,mime FROM downloads WHERE chat_id=1 AND msg_id=7").fetchone()
    assert row is not None
    assert row[0] == "2024-01-01T10:00:00+00:00" and row[1] == 7 and row[2] == 99
    assert row[3] == 5 and row[4] == 2 and row[5] == 4
    assert row[6] == 3 and row[7] == "hello world" and row[8] == "video/mp4"
    lines = (out / "messages.jsonl").read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["mime"] == "video/mp4" and rec["sender_id"] == 7
    assert rec["views"] == 5 and rec["snippet"] == "hello world"
def test_dedup_alias_hash16(tmp_path):
    payload = b"same-bytes-dedup"
    sha = hashlib.sha256(payload).hexdigest()
    h16 = sha[:16]
    async def _dl(m, file=None):
        Path(str(file)).write_bytes(payload)
    c = _cli([_msg(mid=11, name="v.mp4", size=len(payload)), _msg(mid=12, name="v.mp4", size=len(payload))], _dl)
    conn = init_db(":memory:")
    out = tmp_path / "dedup"; out.mkdir()
    r = _run(c, _tgt(cid=1), DownloadOpts(limit=10), out, conn)
    assert r["done"] == 2
    lines = [json.loads(x) for x in (out / "messages.jsonl").read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["sha"] == sha and lines[1]["sha"] == sha
    assert lines[1].get("alias_of") == 11
    assert lines[0]["file"] == build_filename(11, sha, "v.mp4")
    assert h16 in lines[0]["file"] and h16 in lines[1]["file"]
    rows = conn.execute("SELECT msg_id,sha256 FROM downloads WHERE chat_id=1 ORDER BY msg_id").fetchall()
    assert [x[0] for x in rows] == [11, 12]
    assert rows[0][1] == rows[1][1] == sha
def test_sync_checkpoint_persists_and_min_id(tmp_path):
    conn = init_db(":memory:")
    set_sync_checkpoint(conn, 1, 10)
    assert get_sync_checkpoint(conn, 1) == 10
    c = _cli([_msg(mid=11)], _w)
    r = _run(c, _tgt(cid=1), DownloadOpts(limit=10, sync=True), tmp_path, conn)
    assert c.iter_messages.call_args[1]["min_id"] == 10
    assert r["done"] == 1
    assert get_sync_checkpoint(conn, 1) == 11
    c2 = _cli([], None)
    _run(c2, _tgt(cid=1), DownloadOpts(limit=10, sync=True, min_id=20), tmp_path, conn)
    assert c2.iter_messages.call_args[1]["min_id"] == 20
    assert get_sync_checkpoint(conn, 1) == 11


def test_filter_matching_message_helper_properties(tmp_path):
    m_video = SimpleNamespace(id=101, date="2024-01-01", sender_id=1, sender=SimpleNamespace(id=1), media=None, file=SimpleNamespace(name="v.mp4", size=10), video=True, audio=None, photo=None)
    m_audio = SimpleNamespace(id=102, date="2024-01-01", sender_id=1, sender=SimpleNamespace(id=1), media=None, file=SimpleNamespace(name="a.mp3", size=10), video=None, audio=True, photo=None)

    assert _ok(m_video, DownloadOpts(filter="video")) is True
    assert _ok(m_video, DownloadOpts(filter="audio")) is False
    assert _ok(m_audio, DownloadOpts(filter="video")) is False
    assert _ok(m_audio, DownloadOpts(filter="audio")) is True
    assert _ok(m_video, DownloadOpts(filter="photo")) is False


def test_peer_flood_error_exit_code_3(tmp_path):
    from src.cli import main
    from telethon.errors import PeerFloodError

    with patch("src.cli.load_config") as mock_cfg, \
         patch("src.cli.get_client") as mock_get_client, \
         patch("src.cli.resolve_target", new=AsyncMock(return_value=SimpleNamespace(entity=SimpleNamespace(id=1)))), \
         patch("src.cli.init_db", return_value=init_db(":memory:")), \
         patch("src.cli.download_chat", new=AsyncMock(side_effect=PeerFloodError(None))):

        mock_cfg.return_value = SimpleNamespace(session_path="s", api_id=1, api_hash="h", takeout=False)
        dummy_client = AsyncMock()
        dummy_client.__aenter__ = AsyncMock(return_value=dummy_client)
        dummy_client.__aexit__ = AsyncMock(return_value=None)
        mock_get_client.return_value = dummy_client

        exit_code = main(["--target", "@testchan", "--out", str(tmp_path)])
        assert exit_code == 3
