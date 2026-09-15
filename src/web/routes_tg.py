"""Telegram account authentication wizard, explorer, and target resolver routes."""
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from telethon.errors import SessionPasswordNeededError

from src.dialogs import fetch_dialog_rows
from src.resolver import resolve_target
from src.web.client_helpers import _ensure_connected
from src.web.qr import generate_qr_svg
from src.web.security import mask_phone

router = APIRouter()

class PhoneSendCodeRequest(BaseModel):
    phone: str

class PhoneSignInRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: str | None = None

class TwoFactorRequest(BaseModel):
    password: str

class ResolveTargetRequest(BaseModel):
    target: str
    join: bool = False

@router.get("/api/auth/me")
async def get_auth_me(request: Request):
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        return {"authorized": False}
    try:
        await _ensure_connected(client)
        if not await client.is_user_authorized():
            return {"authorized": False}
        me = await client.get_me()
        first_name = getattr(me, "first_name", "") or ""
        last_name = getattr(me, "last_name", "") or ""
        name = f"{first_name} {last_name}".strip() or getattr(me, "username", "User")
        phone = mask_phone(getattr(me, "phone", "") or "")
        return {
            "authorized": True,
            "user": {
                "id": getattr(me, "id", 0),
                "name": name,
                "username": getattr(me, "username", None),
                "phone": phone,
            }
        }
    except Exception as exc:
        return {"authorized": False, "error": str(exc)}

@router.get("/api/auth/qr")
async def get_auth_qr(request: Request):
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        qr = await client.qr_login()
        request.app.state.active_qr = qr
        expires_str = qr.expires.isoformat() if hasattr(qr, "expires") and qr.expires else None
        qr_svg = generate_qr_svg(qr.url)
        return {
            "token": getattr(qr, "token", ""),
            "url": qr.url,
            "svg": qr_svg,
            "expires": expires_str,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.get("/api/auth/qr/status")
async def get_auth_qr_status(request: Request):
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        is_auth = await client.is_user_authorized()
        return {"authorized": bool(is_auth)}
    except Exception as exc:
        return {"authorized": False, "error": str(exc)}

@router.post("/api/auth/phone/send_code")
async def phone_send_code(req: PhoneSendCodeRequest, request: Request):
    phone = req.phone.strip()
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise HTTPException(status_code=400, detail="Invalid E.164 phone number format")
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        res = await client.send_code_request(phone)
        request.app.state.phone_code_hash = res.phone_code_hash
        return {
            "status": "code_sent",
            "phone": mask_phone(phone),
            "phone_code_hash": res.phone_code_hash,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/auth/phone/sign_in")
async def phone_sign_in(req: PhoneSignInRequest, request: Request):
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    code_hash = req.phone_code_hash or getattr(request.app.state, "phone_code_hash", None)
    try:
        await client.sign_in(phone=req.phone.strip(), code=req.code.strip(), phone_code_hash=code_hash)
        return {"status": "authorized"}
    except SessionPasswordNeededError:
        return {"status": "2fa_required"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/auth/2fa")
async def auth_2fa(req: TwoFactorRequest, request: Request):
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        await client.sign_in(password=req.password)
        return {"status": "authorized"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.get("/api/dialogs")
async def list_dialogs(request: Request, limit: int = 100, kind: str = "all", search: str | None = None):
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        raw_rows = await fetch_dialog_rows(client, limit=min(max(1, limit), 500), kind=kind)
        normalized_rows = []
        for r in raw_rows:
            t = str(r.get("type", "group")).lower()
            handle = str(r.get("handle", "") or "")
            username = handle.lstrip("@") if handle else None
            normalized_rows.append({
                "name": r.get("name", "(no name)"),
                "id": r.get("id"),
                "type": t,
                "kind": t,
                "handle": handle,
                "username": username,
            })
        if search and search.strip():
            q = search.strip().lower()
            q_clean = q.lstrip("@")

            def _match(d):
                name = str(d.get("name", "")).lower()
                handle = str(d.get("handle", "")).lower()
                username = str(d.get("username", "") or "").lower()
                chat_id = str(d.get("id", ""))
                return (
                    q in name
                    or q in handle
                    or q_clean in username
                    or q in chat_id
                    or q_clean in handle.lstrip("@")
                )

            normalized_rows = [d for d in normalized_rows if _match(d)]
        return {"dialogs": normalized_rows}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/resolve")
async def resolve(req: ResolveTargetRequest, request: Request):
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        resolved = await resolve_target(client, req.target, join=req.join)
        ent = resolved.entity
        title = getattr(ent, "title", getattr(ent, "first_name", "")) or ""
        return {
            "target": req.target,
            "id": getattr(ent, "id", None),
            "kind": resolved.kind,
            "value": resolved.value,
            "title": title,
            "is_participant": True,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot resolve target: {exc}")
