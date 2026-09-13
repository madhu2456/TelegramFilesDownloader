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
