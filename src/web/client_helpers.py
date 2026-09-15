"""Shared Telethon client utility functions for the web dashboard."""
import inspect
from typing import Any


async def _ensure_connected(client: Any) -> None:
    """Ensure the Telegram client is connected, awaiting is_connected() if it is a coroutine."""
    if client is None:
        return
    conn = client.is_connected()
    if inspect.iscoroutine(conn):
        conn = await conn
    if not conn:
        await client.connect()


async def _resolve_peer_robust(client: Any, chat_id: Any) -> Any:
    """Resolve an integer or string chat_id into a valid Telethon InputPeer or entity."""
    if client is None:
        return chat_id
    try:
        return await client.get_input_entity(chat_id)
    except Exception:
        pass

    try:
        from telethon import utils
        from telethon.tl.types import PeerChannel, PeerChat, PeerUser

        val = int(chat_id)
        candidates: list[Any] = []
        if val > 0:
            candidates.extend([
                int(f"-100{val}"),
                PeerChannel(val),
                PeerChat(val),
                PeerUser(val),
            ])
        else:
            candidates.extend([
                val,
                utils.resolve_id(val)[0],
            ])

        for cand in candidates:
            try:
                return await client.get_input_entity(cand)
            except Exception:
                continue
    except Exception:
        pass

    try:
        return await client.get_entity(chat_id)
    except Exception:
        pass

    return chat_id

