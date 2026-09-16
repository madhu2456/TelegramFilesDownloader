"""Ephemeral token generation, verification, origin validation, rate limiting, and PII masking."""

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from starlette.requests import Request

logger = logging.getLogger("televault.security")

_ACTIVE_TOKEN: str | None = None
_ACTIVE_TOKEN_HASH: bytes | None = None
_TOKEN_SOURCE: str = "unset"


def _set_active_token(token: str, source: str) -> None:
    """Store active token, cache its SHA-256 digest, and record its source."""
    global _ACTIVE_TOKEN, _ACTIVE_TOKEN_HASH, _TOKEN_SOURCE
    cleaned = token.strip()
    _ACTIVE_TOKEN = cleaned
    _ACTIVE_TOKEN_HASH = hashlib.sha256(cleaned.encode("utf-8")).digest()
    _TOKEN_SOURCE = source


def resolve_token_file_path() -> Path:
    """Resolve the master token persistence file path."""
    custom = os.getenv("TELEVAULT_TOKEN_FILE")
    if custom and custom.strip():
        return Path(custom.strip()).resolve()
    app_data = Path("/app/data")
    if app_data.is_dir():
        return app_data / ".token"
    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / "data" / ".token"


def init_security(token_path: Path | None = None) -> str:
    """Initialize security token from env, persisted file, or generate and persist a new one."""
    env_token = os.getenv("TELEVAULT_TOKEN")
    if env_token and env_token.strip():
        _set_active_token(env_token, "env")
        return env_token.strip()

    target_path = token_path if token_path is not None else resolve_token_file_path()
    if target_path.is_file():
        try:
            content = target_path.read_text(encoding="utf-8").strip()
            if content:
                _set_active_token(content, "file")
                return content
        except Exception as e:
            logger.warning("Failed to read existing token file %s: %s", target_path, e)

    new_token = secrets.token_urlsafe(32)
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = target_path.parent / f".token.tmp.{os.getpid()}_{secrets.token_hex(4)}"
        fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(new_token + "\n")
        os.chmod(str(tmp_file), 0o600)
        os.replace(str(tmp_file), str(target_path))
        _set_active_token(new_token, "generated_persisted")
    except OSError as e:
        logger.warning(
            "Could not persist master token to %s (%s). Using ephemeral fallback.",
            target_path,
            e,
        )
        _set_active_token(new_token, "generated_ephemeral_fallback")

    return _ACTIVE_TOKEN or new_token


def generate_ephemeral_token(token_path: Path | None = None) -> str:
    """Backward-compatible alias for init_security."""
    return init_security(token_path=token_path)


def set_ephemeral_token(token: str) -> None:
    """Set the active ephemeral token manually (e.g. for testing)."""
    _set_active_token(token, "manual")


def get_ephemeral_token() -> str | None:
    """Retrieve the currently active ephemeral token."""
    return _ACTIVE_TOKEN


def get_token_source() -> str:
    """Retrieve the source of the currently active token."""
    return _TOKEN_SOURCE


def verify_token(token: str | None) -> bool:
    """Verify the provided token against the active token using timing-safe hash comparison."""
    if not token or not _ACTIVE_TOKEN_HASH:
        return False
    try:
        h_token = hashlib.sha256(str(token).strip().encode("utf-8")).digest()
        return hmac.compare_digest(h_token, _ACTIVE_TOKEN_HASH)
    except Exception:
        return False


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
        custom_hosts = {h.strip().lower() for h in os.getenv("TELEVAULT_ALLOWED_HOSTS", "").split(",") if h.strip()}
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


class AuthFailureTracker:
    """Sliding-window authentication failure rate limiter with bounded memory."""

    def __init__(
        self,
        max_failures: int = 10,
        window_seconds: float = 60.0,
        max_entries: int = 5000,
    ):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_entries = max_entries
        self._failures: dict[str, list[float]] = {}

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        expired = [ip for ip, times in self._failures.items() if not times or times[-1] <= cutoff]
        for ip in expired:
            self._failures.pop(ip, None)

    def is_rate_limited(self, client_ip: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        failures = [t for t in self._failures.get(client_ip, []) if t > cutoff]
        if failures:
            self._failures[client_ip] = failures
        else:
            self._failures.pop(client_ip, None)
        return len(failures) >= self.max_failures

    def record_failure(self, client_ip: str) -> None:
        now = time.time()
        if len(self._failures) >= self.max_entries:
            self._prune(now)
            if len(self._failures) >= self.max_entries:
                oldest_key = min(
                    self._failures,
                    key=lambda k: self._failures[k][0] if self._failures[k] else 0,
                )
                self._failures.pop(oldest_key, None)

        cutoff = now - self.window_seconds
        failures = [t for t in self._failures.get(client_ip, []) if t > cutoff]
        failures.append(now)
        self._failures[client_ip] = failures

    def get_retry_after(self, client_ip: str) -> int:
        failures = self._failures.get(client_ip, [])
        if not failures:
            return int(self.window_seconds)
        oldest = failures[0]
        return max(1, int((oldest + self.window_seconds) - time.time()))

    def reset(self, client_ip: str | None = None) -> None:
        if client_ip:
            self._failures.pop(client_ip, None)
        else:
            self._failures.clear()


_AUTH_RATE_LIMITER = AuthFailureTracker(max_failures=10, window_seconds=60.0, max_entries=5000)


def get_auth_rate_limiter() -> AuthFailureTracker:
    """Retrieve the global authentication rate limiter instance."""
    return _AUTH_RATE_LIMITER


class AuthRateLimiter:
    """Decoupled in-memory rate limiter with per-IP and global quotas."""

    def __init__(self, max_global: int, max_per_ip: int, window_seconds: float = 60.0):
        self.max_global = max_global
        self.max_per_ip = max_per_ip
        self.window_seconds = window_seconds
        self._global_attempts: list[float] = []
        self._ip_attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._global_attempts = [t for t in self._global_attempts if t > cutoff]
        expired_ips = [ip for ip, times in self._ip_attempts.items() if not times or times[-1] <= cutoff]
        for ip in expired_ips:
            self._ip_attempts.pop(ip, None)

    def check_rate_limit(self, client_ip: str) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            self._prune(now)
            cutoff = now - self.window_seconds
            ip_times = [t for t in self._ip_attempts.get(client_ip, []) if t > cutoff]
            if len(self._global_attempts) >= self.max_global:
                oldest = self._global_attempts[0] if self._global_attempts else now
                return True, max(1, int((oldest + self.window_seconds) - now))
            if len(ip_times) >= self.max_per_ip:
                oldest = ip_times[0] if ip_times else now
                return True, max(1, int((oldest + self.window_seconds) - now))
            return False, 0

    def record_attempt(self, client_ip: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(now)
            cutoff = now - self.window_seconds
            self._global_attempts.append(now)
            ip_times = [t for t in self._ip_attempts.get(client_ip, []) if t > cutoff]
            ip_times.append(now)
            self._ip_attempts[client_ip] = ip_times


_PHONE_AUTH_LIMITER = AuthRateLimiter(max_global=5, max_per_ip=2, window_seconds=60.0)
_QR_AUTH_LIMITER = AuthRateLimiter(max_global=20, max_per_ip=10, window_seconds=60.0)


def get_phone_auth_limiter() -> AuthRateLimiter:
    return _PHONE_AUTH_LIMITER


def get_qr_auth_limiter() -> AuthRateLimiter:
    return _QR_AUTH_LIMITER


def get_client_ip(request: Request) -> str:
    """Extract real client IP from reverse proxy headers or socket address."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first_ip = xff.split(",")[0].strip()
        if first_ip:
            return first_ip
    x_real = request.headers.get("x-real-ip")
    if x_real and x_real.strip():
        return x_real.strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"

