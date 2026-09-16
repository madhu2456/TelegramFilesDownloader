"""Cryptographically secure session identification, canonical path jailing, and cookie management."""

import re
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response

SESSION_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{16,64}$")
SESSION_COOKIE_NAME = "televault_session"


def generate_session_id() -> str:
    """Generate a high-entropy cryptographically secure session ID."""
    return secrets.token_urlsafe(32)


def validate_session_id(session_id: str | None) -> bool:
    """Validate format and entropy of a session ID against regex."""
    if not session_id or not isinstance(session_id, str):
        return False
    return bool(SESSION_ID_REGEX.fullmatch(session_id))


def get_canonical_session_path(session_id: str, base_dir: Path | str = "session") -> Path:
    """Resolve and validate the canonical session file path, preventing directory traversal."""
    if not validate_session_id(session_id):
        raise ValueError(f"Invalid session ID: {session_id}")
    base = Path(base_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    p = (base / f"{session_id}.session").resolve()
    if not p.is_relative_to(base):
        raise ValueError("Session path traversal detected")
    return p


def get_canonical_out_dir(session_id: str, base_out_dir: Path | str = "out") -> Path:
    """Resolve and validate the canonical tenant output directory, preventing traversal."""
    if not validate_session_id(session_id):
        raise ValueError(f"Invalid session ID: {session_id}")
    base = (Path(base_out_dir) / "sessions").resolve()
    base.mkdir(parents=True, exist_ok=True)
    d = (base / session_id).resolve()
    if not d.is_relative_to(base):
        raise ValueError("Output directory traversal detected")
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_session_id_from_request(request: Request) -> str | None:
    """Extract and validate the session ID cookie from the request."""
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    return sid if validate_session_id(sid) else None


def set_session_cookie(response: Response, session_id: str, secure: bool = False) -> None:
    """Set the HttpOnly SameSite=Lax session cookie on response."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=2592000,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Clear the session cookie on response."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
    )
