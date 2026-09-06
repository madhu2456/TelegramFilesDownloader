"""Validated configuration loader with strict file permissions."""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
os.umask(0o077)
_HINT = "Get api_id/api_hash at https://my.telegram.org"
_REDACT = {"api_hash", "phone", "code", "auth_key", "qr_token"}
@dataclass
class Config:
    api_id: int
    api_hash: str
    phone: str
    session_path: Path
    takeout: bool = False
def scrub_for_log(data: dict) -> dict:
    return {k: ("[REDACTED]" if str(k).lower() in _REDACT else v) for k, v in data.items()}
def validate_config(cfg: Config) -> None:
    ok = type(cfg.api_id) is int and cfg.api_id > 0
    ok &= bool(re.fullmatch(r"[0-9a-fA-F]{32}", cfg.api_hash or ""))
    ok &= bool(re.fullmatch(r"\+[1-9]\d{7,14}", cfg.phone or ""))
    if not ok:
        raise SystemExit(f"Invalid config {scrub_for_log(cfg.__dict__)}: {_HINT}")
def ensure_out_dir(p) -> Path:
    d = Path(p)
    d.mkdir(parents=True, exist_ok=True)
    if d.stat().st_mode & 0o002:
        raise SystemExit(f"Refusing world-writable dir: {d}")
    return d
def load_config(dotenv_path=None) -> Config:
    load_dotenv(dotenv_path) if dotenv_path else load_dotenv()
    try:
        api_id = int(str(os.getenv("TG_API_ID", "")).strip())
    except ValueError:
        raise SystemExit(f"Invalid TG_API_ID: {_HINT}")
    cfg = Config(api_id, str(os.getenv("TG_API_HASH", "")).strip(), str(os.getenv("TG_PHONE", "")).strip(), Path(os.getenv("TG_SESSION", "session/telegram.session") or "session/telegram.session"))
    validate_config(cfg)
    return cfg
