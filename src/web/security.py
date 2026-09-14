"""Ephemeral token generation, verification, origin validation, and PII masking."""
import hmac
import os
import secrets
from urllib.parse import urlparse

_ACTIVE_TOKEN: str | None = None

def generate_ephemeral_token() -> str:
    """Generate and store a new cryptographically secure 32-byte URL-safe token.
    If TELEVAULT_TOKEN environment variable is set, uses that instead.
    """
    global _ACTIVE_TOKEN
    env_token = os.getenv("TELEVAULT_TOKEN")
    if env_token:
        _ACTIVE_TOKEN = env_token.strip()
    else:
        _ACTIVE_TOKEN = secrets.token_urlsafe(32)
    return _ACTIVE_TOKEN

def get_ephemeral_token() -> str | None:
    """Retrieve the currently active ephemeral token."""
    return _ACTIVE_TOKEN

def set_ephemeral_token(token: str) -> None:
    """Set the active ephemeral token manually (e.g. for testing)."""
    global _ACTIVE_TOKEN
    _ACTIVE_TOKEN = token

def verify_token(token: str | None) -> bool:
    """Verify the provided token against the active token using constant-time comparison."""
    if not token or not _ACTIVE_TOKEN:
        return False
    return hmac.compare_digest(str(token), str(_ACTIVE_TOKEN))

def verify_origin(origin: str | None, port: int) -> bool:
    """Verify that Origin header matches 127.0.0.1, localhost, [::1], televault.madhudadi.in,
    or TELEVAULT_ALLOWED_HOSTS.
    If origin is None, returns True for same-origin direct calls.
    """
    if origin is None:
        return True
    try:
        parsed = urlparse(origin)
        host = parsed.hostname
        if not host:
            return False
        h_lower = host.lower()
        loopback_hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
        custom_hosts = {
            h.strip().lower()
            for h in os.getenv("TELEVAULT_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        }
        allowed_domains = {"televault.madhudadi.in"}.union(custom_hosts)
        p = parsed.port or (80 if parsed.scheme == "http" else 443)

        if h_lower in loopback_hosts:
            return p == port
        if h_lower in allowed_domains:
            # Reverse proxy TLS port 443, standard 80, or active port
            return p in (443, 80, port)
        return False
    except Exception:
        return False

def mask_phone(phone: str | None) -> str:
    """Mask phone number to protect PII, e.g. +15551234567 -> +1***4567."""
    if not phone or not isinstance(phone, str):
        return ""
    p = phone.strip()
    if len(p) <= 6:
        return (p[0] + "***" + p[-1]) if len(p) >= 2 else "***"
    return f"{p[:2]}***{p[-4:]}"
