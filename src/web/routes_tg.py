"""Telegram account authentication wizard, explorer, and target resolver routes."""
import re
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from telethon.errors import SessionPasswordNeededError

from src.dialogs import fetch_dialog_rows
from src.resolver import resolve_target
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
        if not client.is_connected():
            await client.connect()
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
    if not client.is_connected():
        await client.connect()
    try:
        qr = await client.qr_login()
        expires_str = qr.expires.isoformat() if hasattr(qr, "expires") and qr.expires else None
        return {
            "token": getattr(qr, "token", ""),
            "url": qr.url,
            "expires": expires_str,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/auth/phone/send_code")
async def phone_send_code(req: PhoneSendCodeRequest, request: Request):
    phone = req.phone.strip()
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise HTTPException(status_code=400, detail="Invalid E.164 phone number format")
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    if not client.is_connected():
        await client.connect()
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
    if not client.is_connected():
        await client.connect()
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
    if not client.is_connected():
        await client.connect()
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
    if not client.is_connected():
        await client.connect()
    try:
        rows = await fetch_dialog_rows(client, limit=min(max(1, limit), 500), kind=kind)
        if search and search.strip():
            q = search.strip().lower()
            rows = [r for r in rows if q in str(r.get("name", "")).lower() or q in str(r.get("username", "")).lower()]
        return {"dialogs": rows}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/resolve")
async def resolve(req: ResolveTargetRequest, request: Request):
    client = getattr(request.app.state, "tg_client", None)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    if not client.is_connected():
        await client.connect()
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
