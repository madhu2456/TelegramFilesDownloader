"""Telegram account authentication wizard, explorer, and target resolver routes."""

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response
from telethon.errors import SessionPasswordNeededError

from src.dialogs import fetch_dialog_rows
from src.resolver import resolve_target
from src.web.client_helpers import _ensure_connected, get_session_client, get_tenant_session
from src.web.qr import generate_qr_svg
from src.web.security import get_client_ip, get_phone_auth_limiter, get_qr_auth_limiter, mask_phone
from src.web.session_security import clear_session_cookie, get_session_id_from_request

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
    client = await get_session_client(request, allow_jit=False)
    if not client:
        return {"authorized": False}
    try:
        await _ensure_connected(client)
        if not await client.is_user_authorized():
            return {"authorized": False}
        tenant = get_tenant_session(request)
        if tenant:
            tenant.is_authorized = True
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
            },
        }
    except Exception as exc:
        return {"authorized": False, "error": str(exc)}


@router.get("/api/auth/qr")
async def get_auth_qr(request: Request):
    client_ip = get_client_ip(request)
    limited, retry_after = get_qr_auth_limiter().check_rate_limit(client_ip)
    if limited:
        return JSONResponse(
            status_code=429,
            content={"error": "TOO_MANY_REQUESTS", "detail": "QR login rate limit exceeded. Please wait."},
            headers={"Retry-After": str(retry_after)},
        )
    get_qr_auth_limiter().record_attempt(client_ip)

    client = await get_session_client(request, allow_jit=True)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        qr = await client.qr_login()
        tenant = get_tenant_session(request)
        if tenant:
            tenant.active_qr = qr
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
    client = await get_session_client(request, allow_jit=False)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        is_auth = await client.is_user_authorized()
        if is_auth:
            tenant = get_tenant_session(request)
            if tenant:
                tenant.is_authorized = True
        return {"authorized": bool(is_auth)}
    except Exception as exc:
        return {"authorized": False, "error": str(exc)}


@router.post("/api/auth/phone/send_code")
async def phone_send_code(req: PhoneSendCodeRequest, request: Request):
    phone = req.phone.strip()
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise HTTPException(status_code=400, detail="Invalid E.164 phone number format")
    client_ip = get_client_ip(request)
    limited, retry_after = get_phone_auth_limiter().check_rate_limit(client_ip)
    if limited:
        return JSONResponse(
            status_code=429,
            content={"error": "TOO_MANY_REQUESTS", "detail": "Phone verification rate limit exceeded. Please wait."},
            headers={"Retry-After": str(retry_after)},
        )
    get_phone_auth_limiter().record_attempt(client_ip)

    client = await get_session_client(request, allow_jit=True)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        res = await client.send_code_request(phone)
        tenant = get_tenant_session(request)
        if tenant:
            tenant.phone_code_hash = res.phone_code_hash
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
    client = await get_session_client(request, allow_jit=False)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    tenant = get_tenant_session(request)
    code_hash = req.phone_code_hash or (
        tenant.phone_code_hash if tenant else getattr(request.app.state, "phone_code_hash", None)
    )
    try:
        await client.sign_in(phone=req.phone.strip(), code=req.code.strip(), phone_code_hash=code_hash)
        if tenant:
            tenant.is_authorized = True
        return {"status": "authorized"}
    except SessionPasswordNeededError:
        return {"status": "2fa_required"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/auth/2fa")
async def auth_2fa(req: TwoFactorRequest, request: Request):
    client = await get_session_client(request, allow_jit=False)
    if not client:
        raise HTTPException(status_code=500, detail="Telegram client not initialized")
    await _ensure_connected(client)
    try:
        await client.sign_in(password=req.password)
        tenant = get_tenant_session(request)
        if tenant:
            tenant.is_authorized = True
        return {"status": "authorized"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/dialogs")
async def list_dialogs(request: Request, limit: int = 100, kind: str = "all", search: str | None = None):
    client = await get_session_client(request, allow_jit=False)
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
            normalized_rows.append(
                {
                    "name": r.get("name", "(no name)"),
                    "id": r.get("id"),
                    "type": t,
                    "kind": t,
                    "handle": handle,
                    "username": username,
                }
            )
        if search and search.strip():
            q = search.strip().lower()
            q_clean = q.lstrip("@")

            def _match(d):
                name = str(d.get("name", "")).lower()
                handle = str(d.get("handle", "")).lower()
                username = str(d.get("username", "") or "").lower()
                chat_id = str(d.get("id", ""))
                return q in name or q in handle or q_clean in username or q in chat_id or q_clean in handle.lstrip("@")

            normalized_rows = [d for d in normalized_rows if _match(d)]
        return {"dialogs": normalized_rows}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/resolve")
async def resolve(req: ResolveTargetRequest, request: Request):
    client = await get_session_client(request, allow_jit=False)
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


@router.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    session_id = get_session_id_from_request(request)
    if session_id:
        sm = getattr(request.app.state, "session_manager", None)
        if sm:
            await sm.logout_tenant(session_id)
        clear_session_cookie(response)
    return {"status": "logged_out"}
