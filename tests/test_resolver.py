"""Resolver tests: AsyncMock only, no network."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from telethon.errors import ChannelPrivateError, InviteHashExpiredError, InviteHashInvalidError, UsernameNotOccupiedError
from telethon.tl.functions.channels import JoinChannelRequest
from src.resolver import ResolveError, parse_target, resolve_target
def _cli(ent=None):
    c = AsyncMock()
    e = ent or MagicMock(id=7)
    c.get_entity.return_value = e
    c.get_me.return_value = MagicMock(id=9)
    async def _call(req):
        return MagicMock()
    c.side_effect = _call
    return c, e
def test_parse_target_seven_forms():
    assert parse_target("12345") == {"kind": "id", "value": 12345}
    assert parse_target("+15551234567") == {"kind": "phone", "value": "+15551234567"}
    assert parse_target("https://t.me/c/123456/7") == {"kind": "private_channel", "value": -100123456}
    assert parse_target("https://t.me/joinchat/AAAAbbbb") == {"kind": "invite", "value": "AAAAbbbb"}
    assert parse_target("https://t.me/+AAAAbbbb") == {"kind": "invite", "value": "AAAAbbbb"}
    assert parse_target("https://t.me/someuser123") == {"kind": "username", "value": "someuser123"}
    assert parse_target("@someuser123") == {"kind": "username", "value": "someuser123"}
    assert parse_target("-1001234567890")["kind"] == "id"
def test_parse_target_rejects_bad():
    with pytest.raises(ResolveError) as a:
        parse_target("not a target!!!")
    assert a.value.code == "BAD_FORMAT"
    assert "unrecognized" in str(a.value).lower()
    with pytest.raises(ResolveError) as b:
        parse_target("   ")
    assert b.value.code == "EMPTY"
    assert "empty" in str(b.value).lower()
def test_join_false_never_calls_join():
    c, e = _cli()
    r = asyncio.run(resolve_target(c, "@someuser123", join=False))
    joins = [x for x in c.mock_calls if "JoinChannel" in str(x)]
    assert joins == []
    assert r.kind == "username" and r.value == "someuser123"
    assert JoinChannelRequest.__name__ not in str(c.mock_calls)
    assert e is r.entity
def test_taxonomy_expired_invalid():
    for exc in (InviteHashExpiredError(None), InviteHashInvalidError(None)):
        c = AsyncMock()
        c.get_entity.side_effect = exc
        with pytest.raises(ResolveError) as e:
            asyncio.run(resolve_target(c, "https://t.me/+AAAAbbbb", join=True))
        assert e.value.code == "INVITE_EXPIRED"
        assert "invite" in str(e.value).lower()
    assert parse_target("https://t.me/+AAAAbbbb")["kind"] == "invite"
    assert issubclass(InviteHashExpiredError, Exception)
    assert issubclass(InviteHashInvalidError, Exception)
def test_taxonomy_private_exit5_nojoin():
    c = AsyncMock()
    c.get_entity.side_effect = ChannelPrivateError(None)
    with pytest.raises(ResolveError) as e:
        asyncio.run(resolve_target(c, "@someuser123", join=False))
    assert e.value.code == "NOT_PARTICIPANT"
    assert "participant" in str(e.value).lower() or "private" in str(e.value).lower()
    c2 = AsyncMock()
    c2.get_entity.side_effect = UsernameNotOccupiedError(None)
    with pytest.raises(ResolveError) as e2:
        asyncio.run(resolve_target(c2, "@nouser12345", join=False))
    assert e2.value.code == "EXIT5"
    assert "missing" in str(e2.value).lower() or "nouser" in str(e2.value).lower()
    c3, _ = _cli()
    with pytest.raises(ResolveError) as e3:
        asyncio.run(resolve_target(c3, "https://t.me/+AAAAbbbb", join=False))
    assert e3.value.code == "INVITE_NO_JOIN"
    assert "join" in str(e3.value).lower()
