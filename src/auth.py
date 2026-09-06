"""Hardened Telegram authentication helpers (no secrets in logs)."""
import getpass
import logging
import os
from pathlib import Path
from telethon.errors import SessionPasswordNeededError
from .config import scrub_for_log
log = logging.getLogger(__name__)
def ensure_session_file(p) -> Path:
    path = Path(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(str(path), 0o600)
    return path
def prompt_2fa() -> str:
    return getpass.getpass("Enter 2FA password: ")
async def login_with_phone(client, phone: str) -> None:
    if not phone:
        raise ValueError("phone required")
    log.info("phone login: %s", scrub_for_log({"phone": phone}))
    await client.send_code_request(phone)
    code = getpass.getpass("Enter Telegram login code: ")
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        await client.sign_in(password=prompt_2fa())
async def login_with_qr(client) -> None:
    qr = await client.qr_login()
    log.info("qr login: %s", scrub_for_log({"qr_token": "pending"}))
    await qr.wait()
async def logout_and_revoke(client) -> None:
    await client.log_out()
    log.info("logged out")
