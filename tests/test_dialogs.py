"""Dialog listing tests: AsyncMock get_dialogs only, no network."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from src.dialogs import classify_dialog, fetch_dialog_rows, format_dialog_table
class User(SimpleNamespace):
    pass
class Channel(SimpleNamespace):
    pass
class Chat(SimpleNamespace):
    pass
def _dlg(name, ent):
    d = MagicMock()
    d.name = name
    d.entity = ent
    return d
def _mk():
    bc = Channel(id=11, title="News Daily", broadcast=True, username="newsdaily")
    grp = Channel(id=22, title="Fam Group", broadcast=False, megagroup=True, username=None)
    dm = User(id=33, first_name="Alice", last_name="A", username="alice12345", phone="+15551234567")
    small = Chat(id=44, title="Small Team")
    return [_dlg("News Daily", bc), _dlg("Fam Group", grp), _dlg("Alice A", dm), _dlg("Small Team", small)]
def _cli(dialogs):
    c = AsyncMock()
    c.get_dialogs.return_value = dialogs
    return c
def test_all_filter_classify_and_no_phone_leak():
    c = _cli(_mk())
    rows = asyncio.run(fetch_dialog_rows(c, limit=100, kind="all"))
    assert len(rows) == 4
    by_id = {r["id"]: r for r in rows}
    assert by_id[11]["type"] == "channel"
    assert by_id[22]["type"] == "group"
    assert by_id[33]["type"] == "dm"
    assert by_id[44]["type"] == "group"
    table = format_dialog_table(rows)
    assert "name | id | type | username/link" in table
    assert "+15551234567" not in table
    assert "15551234567" not in table
    assert classify_dialog(None) == "dm"
def test_group_filter_and_limit_bound():
    rows = asyncio.run(fetch_dialog_rows(_cli(_mk()), limit=100, kind="group"))
    assert len(rows) == 2
    assert all(r["type"] == "group" for r in rows)
    assert all("alice12345" not in r["handle"] for r in rows)
    c2 = _cli(_mk())
    rows2 = asyncio.run(fetch_dialog_rows(c2, limit=1, kind="all"))
    assert len(rows2) == 1
    assert rows2[0]["name"] == "News Daily"
    assert c2.get_dialogs.call_args[1].get("limit") == 1
    table2 = format_dialog_table(rows2)
    assert "+1555" not in table2
    assert "News Daily" in table2
