"""Dialog listing: classify + scrubbed table (never phones/secrets)."""
from typing import Any
def classify_dialog(entity: Any) -> str:
    """group = megagroup/small group, channel = broadcast, dm = User."""
    if entity is None:
        return "dm"
    t = type(entity).__name__
    if t == "User":
        return "dm"
    if t == "Channel":
        return "channel" if bool(getattr(entity, "broadcast", False)) else "group"
    return "group"
def safe_handle(entity: Any) -> str:
    """Return @username or empty; never phone/link secrets."""
    u = getattr(entity, "username", None)
    if isinstance(u, str) and u.strip():
        s = u.strip().lstrip("@")
        if 5 <= len(s) <= 32 and s.replace("_", "").isalnum():
            return "@" + s
    return ""
def safe_name(dialog: Any, entity: Any) -> str:
    for cand in (getattr(dialog, "name", None), getattr(entity, "title", None)):
        if isinstance(cand, str) and cand.strip():
            return cand.strip()[:120]
    full = (str(getattr(entity, "first_name", None) or "") + " " + str(getattr(entity, "last_name", None) or "")).strip()
    return full[:120] if full else "(no name)"
async def fetch_dialog_rows(client: Any, limit: int = 100, kind: str = "all") -> list:
    """get_dialogs(limit) -> scrubbed rows (name,id,type,handle)."""
    if client is None:
        raise ValueError("bad client")
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 100
    n = max(1, min(n, 1000))
    k = str(kind or "all").lower()
    if k not in ("all", "group", "channel", "dm"):
        k = "all"
    rows = []
    for d in ((await client.get_dialogs(limit=n)) or [])[:n]:
        ent = getattr(d, "entity", None)
        t = classify_dialog(ent)
        if k != "all" and t != k:
            continue
        rows.append({"name": safe_name(d, ent), "id": getattr(ent, "id", "?"), "type": t, "handle": safe_handle(ent)})
    return rows
def format_dialog_table(rows: list) -> str:
    lines = ["name | id | type | username/link"]
    for r in rows or []:
        lines.append(f"{r.get('name', '')} | {r.get('id', '?')} | {r.get('type', '')} | {r.get('handle', '')}")
    return "\n".join(lines)
