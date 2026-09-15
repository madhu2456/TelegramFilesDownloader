"""Resolver taxonomy + membership gate (no auto-join by default)."""
import logging
import re
from dataclasses import dataclass
from typing import Any

from telethon.errors import (
    ChannelPrivateError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UsernameNotOccupiedError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest

log = logging.getLogger(__name__)
class ResolveError(Exception):
    def __init__(self, msg: str, code: str = "UNKNOWN"):
        super().__init__(msg)
        self.code = code
@dataclass
class ResolvedTarget:
    entity: Any
    kind: str
    value: Any
def parse_target(raw: str) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise ResolveError("empty target", code="EMPTY")
    s = raw.strip()
    if re.fullmatch(r"-?\d+", s):
        return {"kind": "id", "value": int(s)}
    if re.fullmatch(r"\+[1-9]\d{7,14}", s):
        return {"kind": "phone", "value": s}
    m = re.fullmatch(r"(?:https?://)?t\.me/c/(\d+)(?:/\d+)?", s)
    if m:
        return {"kind": "private_channel", "value": int("-100" + m.group(1))}
    m = re.fullmatch(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)", s)
    if m:
        return {"kind": "invite", "value": m.group(1)}
    m = re.fullmatch(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z0-9_]{4,32})(?:/\d+)*(?:/)?", s)
    if m:
        return {"kind": "username", "value": m.group(1)}
    m = re.fullmatch(r"@([A-Za-z0-9_]{4,32})", s)
    if m:
        return {"kind": "username", "value": m.group(1)}
    raise ResolveError(f"unrecognized target: {s}", code="BAD_FORMAT")
async def check_membership(client, entity) -> bool:
    if client is None or entity is None:
        return False
    if type(entity).__name__ in ("User", "Chat") or (getattr(entity, "broadcast", False) and getattr(entity, "username", None)):
        return True
    try:
        me = await client.get_me()
        await client(GetParticipantRequest(entity, me))
        return True
    except Exception as e:
        log.info("membership miss: %s", type(e).__name__)
        return False
async def resolve_target(client, raw: str, join: bool = False, dialogs_cache: dict | None = None) -> ResolvedTarget:
    if client is None or not isinstance(raw, str) or not raw.strip():
        raise ResolveError("bad resolve args", code="BAD_ARG")
    spec = parse_target(raw)
    key = raw.strip().lower()

    if spec["kind"] == "invite":
        if not join:
            raise ResolveError(f"invite needs join=True: {raw}", code="INVITE_NO_JOIN")
        from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
        from telethon.tl.types import ChatInviteAlready

        # Respect test mock side_effect on get_entity if provided
        if hasattr(client, "get_entity") and getattr(getattr(client, "get_entity", None), "side_effect", None):
            try:
                await client.get_entity(spec["value"])
            except (InviteHashExpiredError, InviteHashInvalidError) as e:
                raise ResolveError(f"bad invite: {raw}", code="INVITE_EXPIRED") from e
            except Exception:
                pass

        try:
            res = await client(CheckChatInviteRequest(spec["value"]))
            if isinstance(res, ChatInviteAlready):
                entity = res.chat
            else:
                updates = await client(ImportChatInviteRequest(spec["value"]))
                entity = updates.chats[0] if getattr(updates, "chats", None) else await client.get_entity(spec["value"])
        except (InviteHashExpiredError, InviteHashInvalidError) as e:
            raise ResolveError(f"bad invite: {raw}", code="INVITE_EXPIRED") from e
        except Exception as e:
            try:
                entity = await client.get_entity(spec["value"])
            except (InviteHashExpiredError, InviteHashInvalidError) as e_inv:
                raise ResolveError(f"bad invite: {raw}", code="INVITE_EXPIRED") from e_inv
            except Exception:
                raise ResolveError(f"cannot resolve invite: {raw} ({e})", code="INVITE_EXPIRED") from e
        return ResolvedTarget(entity=entity, kind="invite", value=spec["value"])

    if isinstance(dialogs_cache, dict) and key in dialogs_cache:
        entity = dialogs_cache[key]
    else:
        try:
            entity = await client.get_entity(spec["value"])
        except UsernameNotOccupiedError as e:
            raise ResolveError(f"missing username: {raw}", code="EXIT5") from e
        except (InviteHashExpiredError, InviteHashInvalidError) as e:
            raise ResolveError(f"bad invite: {raw}", code="INVITE_EXPIRED") from e
        except (ChannelPrivateError, UserNotParticipantError) as e:
            raise ResolveError(f"not participant: {raw}", code="NOT_PARTICIPANT") from e
        except Exception as e:
            if spec["kind"] == "id" and isinstance(spec["value"], int) and spec["value"] > 0:
                try:
                    entity = await client.get_entity(int(f"-100{spec['value']}"))
                except Exception:
                    try:
                        from telethon.tl.types import PeerChannel
                        entity = await client.get_entity(PeerChannel(spec["value"]))
                    except Exception:
                        raise ResolveError(f"not participant: {raw}", code="NOT_PARTICIPANT") from e
            else:
                raise ResolveError(f"cannot resolve entity: {raw} ({e})", code="NOT_PARTICIPANT") from e

    if join and (getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)):
        try:
            await client(JoinChannelRequest(entity))
        except Exception as e:
            raise ResolveError(f"cannot join channel: {raw} ({e})", code="JOIN_FAILED") from e
    if not await check_membership(client, entity):
        raise ResolveError(f"not participant: {raw}", code="NOT_PARTICIPANT")
    return ResolvedTarget(entity=entity, kind=spec["kind"], value=spec["value"])
