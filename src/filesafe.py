"""File safety: sanitize, O_EXCL create, atomic replace, disk check."""
import errno
import hashlib
import os
from pathlib import Path

os.umask(0o077)
_ALLOW = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


def sanitize_component(n):
    s = str(n or "")
    if "\x00" in s:
        raise ValueError("NUL byte")
    s = os.path.basename(s.replace("\\", "/"))
    s = "".join(c if c in _ALLOW else "_" for c in s)
    s = s.strip("._") or "file"
    if s in (".", ".."):
        return "file"
    return s[:100] or "file"


def build_filename(msg_id, sha, raw):
    h = "".join(c for c in str(sha).lower() if c in "0123456789abcdef")
    hash8 = (h + "0" * 16)[:16]
    safe = sanitize_component(raw or "file")
    return f"{int(msg_id)}_{hash8}_{safe}"


def exclusive_create(p):
    return os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)


def atomic_replace(tmp, dst, sz, sha):
    t, d = Path(tmp), Path(dst)
    if t.stat().st_size != int(sz):
        raise ValueError("size mismatch")
    h = hashlib.sha256()
    with open(t, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    if h.hexdigest() != str(sha).lower():
        raise ValueError("sha mismatch")
    with open(t, "rb") as f:
        os.fsync(f.fileno())
    os.replace(str(t), str(d))
    fd = os.open(str(d.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def precheck_disk(out, need):
    d = Path(out)
    base = d if d.is_dir() else d.parent
    base.mkdir(parents=True, exist_ok=True)
    v = os.statvfs(str(base))
    if v.f_bavail * v.f_frsize < int(need):
        raise OSError(errno.ENOSPC, "no space")


def assert_safe_out(out):
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    if d.stat().st_mode & 0o002:
        raise SystemExit(f"Refusing world-writable dir: {d}")
    return d
