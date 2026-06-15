"""
Lazada OAuth helper routes — mount in main.py:
    from app.lazada_auth import router as lazada_auth_router
    app.include_router(lazada_auth_router, prefix="/api/lazada")

Handles:
  - GET  /api/lazada/auth-url    -> generate authorization URL
  - GET  /api/lazada/callback    -> catch redirect, exchange code for tokens
  - POST /api/lazada/refresh     -> manually refresh token using saved/provided refresh token
  - GET  /api/lazada/status      -> show stored token status
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from app.security_utils import make_oauth_state, secure_json_load, secure_json_save, verify_oauth_state

router = APIRouter()
log = logging.getLogger("app.lazada_auth")

AUTH_HOST = "https://auth.lazada.com"
AUTH_API_HOST = "https://auth.lazada.com/rest"
TOKEN_PATH = "/auth/token/create"
REFRESH_PATH = "/auth/token/refresh"

# Keep token storage aligned with LazadaScraper.
TOKENS_FILE = Path("/app/sessions/lazada.json")


def _cfg() -> tuple[str, str, str]:
    app_key = (os.getenv("LAZADA_APP_KEY") or "").strip()
    app_secret = (os.getenv("LAZADA_APP_SECRET") or "").strip()
    redirect = (os.getenv("LAZADA_REDIRECT_URL") or "https://192.168.50.129:8080/api/lazada/callback").strip()
    return app_key, app_secret, redirect


def _validate_https_redirect(redirect: str) -> None:
    parsed = urlparse(redirect)
    allow_insecure = (os.getenv("ALLOW_INSECURE_REDIRECT_URI", "false") or "false").strip().lower() == "true"
    if parsed.scheme == "https" and parsed.netloc:
        return
    if allow_insecure:
        return
    raise HTTPException(500, "LAZADA_REDIRECT_URL must use https in secure mode")


def _must_have_config() -> None:
    app_key, app_secret, redirect = _cfg()
    if not app_key or not app_secret:
        raise HTTPException(500, "Missing LAZADA_APP_KEY or LAZADA_APP_SECRET")
    _validate_https_redirect(redirect)


def _sign(path: str, params: dict[str, str]) -> str:
    _, app_secret, _ = _cfg()
    sorted_kv = "".join(f"{k}{v}" for k, v in sorted(params.items()) if k != "sign")
    base = path + sorted_kv
    return hmac.new(app_secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest().upper()


def _load_tokens() -> dict:
    _, app_secret, _ = _cfg()
    return secure_json_load(TOKENS_FILE, secret_hint=app_secret)


def _env_auth_code() -> str:
    return (os.getenv("LAZADA_AUTH_CODE") or "").strip()


def _save_tokens(data: dict) -> None:
    _, app_secret, _ = _cfg()
    secure_json_save(TOKENS_FILE, data, secret_hint=app_secret)


def _error_page(title: str, message: str, status_code: int = 400) -> HTMLResponse:
    safe_title = title.strip() or "Request Failed"
    safe_message = message.strip() or "Request could not be completed."
    return HTMLResponse(
        f"""
        <html><body style="font-family:monospace;padding:40px;background:#0d0f12;color:#ef4444">
        <h2>{safe_title}</h2>
        <p>{safe_message}</p>
        </body></html>
        """,
        status_code=status_code,
    )


def _mask(value: str, keep_start: int = 8, keep_end: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep_start + keep_end:
        return "*" * len(value)
    return f"{value[:keep_start]}...{value[-keep_end:]}"


async def _token_create(code: str) -> dict:
    app_key, _, _ = _cfg()
    params = {
        "app_key": app_key,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "code": code,
    }
    params["sign"] = _sign(TOKEN_PATH, params)
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{AUTH_API_HOST}{TOKEN_PATH}", params=params, timeout=30)
    return r.json()


async def _token_refresh(refresh_token: str) -> dict:
    app_key, _, _ = _cfg()
    params = {
        "app_key": app_key,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "refresh_token": refresh_token,
    }
    params["sign"] = _sign(REFRESH_PATH, params)
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{AUTH_API_HOST}{REFRESH_PATH}", params=params, timeout=30)
    return r.json()


@router.get("/auth-url")
def get_auth_url(state: str = "inventory-sync"):
    """Generate Lazada authorization URL. Open in browser to authorize."""
    _must_have_config()
    app_key, app_secret, redirect = _cfg()
    signed_state = make_oauth_state(
        secret_hint=app_secret,
        provider="lazada",
        context={"label": (state or "inventory-sync")[:120]},
    )
    encoded_redirect = quote(redirect, safe="")
    url = (
        f"{AUTH_HOST}/oauth/authorize"
        f"?response_type=code"
        f"&force_auth=true"
        f"&client_id={app_key}"
        f"&redirect_uri={encoded_redirect}"
        f"&state={quote(signed_state, safe='')}"
    )
    return {
        "url": url,
        "redirect_uri": redirect,
        "state": signed_state,
        "note": "Open this URL in your browser and approve the Lazada app.",
    }


@router.get("/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Lazada redirects here after seller authorizes. Exchanges code for tokens."""
    _must_have_config()
    _, app_secret, _ = _cfg()
    if error or not code:
        return _error_page("Authorization Failed", "Authorization was rejected or missing code.", 400)

    state_ok, state_ctx = verify_oauth_state(state, secret_hint=app_secret, provider="lazada")
    if not state_ok:
        return _error_page("Authorization Failed", "Invalid or expired OAuth state. Please start login again.", 400)

    try:
        data = await _token_create(code)
    except Exception:
        log.exception("[Lazada OAuth] token exchange transport error")
        return _error_page("Token Exchange Error", "Unable to reach Lazada token service. Please retry.", 500)

    if str(data.get("code", "0")) != "0" or not data.get("access_token"):
        log.warning("[Lazada OAuth] token exchange rejected code=%s", data.get("code"))
        return _error_page("Token Exchange Failed", "Lazada rejected the authorization code. Please restart authorization.", 400)

    token_record = {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "account": data.get("account", ""),
        "country": data.get("country", ""),
        "expires_in": data.get("expires_in", 0),
        "refresh_expires_in": data.get("refresh_expires_in", 0),
        "fetched_at": int(time.time()),
    }
    _save_tokens(token_record)

    return HTMLResponse(
        f"""
        <html><body style="font-family:monospace;padding:40px;background:#0d0f12;color:#c8cdd8">
        <h2 style="color:#22c55e">Authorization Successful</h2>
        <p><b>Account:</b> {token_record.get('account') or '(unknown)'}</p>
        <p><b>Country:</b> {token_record.get('country') or '(unknown)'}</p>
        <p><b>State:</b> {state_ctx.get('label') or '(ok)'}</p>
        <p><b>Access token:</b> {_mask(token_record.get('access_token', ''))}</p>
        <p><b>Refresh token:</b> {_mask(token_record.get('refresh_token', ''))}</p>
        <p style="color:#f59e0b">You can close this tab. Token has been saved.</p>
        </body></html>
        """
    )


@router.get("/status")
def status():
    rec = _load_tokens()
    if not rec:
        return {
            "authorized": False,
            "note": "No Lazada token file found. Authorize via /api/lazada/auth-url.",
        }

    fetched_at = int(rec.get("fetched_at") or 0)
    expires_in = int(rec.get("expires_in") or 0)
    age = max(0, int(time.time()) - fetched_at) if fetched_at else None
    remaining = max(0, expires_in - age) if age is not None else None
    return {
        "authorized": bool(rec.get("access_token")),
        "account": rec.get("account", ""),
        "country": rec.get("country", ""),
        "fetched_at": fetched_at,
        "age_seconds": age,
        "access_expires_in_seconds": expires_in,
        "access_remaining_seconds": remaining,
        "has_refresh_token": bool(rec.get("refresh_token")),
        "access_token_preview": _mask(rec.get("access_token", "")),
        "refresh_token_preview": _mask(rec.get("refresh_token", "")),
    }


@router.post("/refresh")
async def refresh(payload: dict | None = None):
    """Refresh Lazada access token using provided or saved refresh token."""
    _must_have_config()
    body = payload or {}
    refresh_token = str(body.get("refresh_token") or "").strip()
    if not refresh_token:
        refresh_token = str(_load_tokens().get("refresh_token") or "").strip()
    if not refresh_token:
        raise HTTPException(400, "Missing refresh_token and no saved refresh token found")

    data = await _token_refresh(refresh_token)
    if str(data.get("code", "0")) != "0" or not data.get("access_token"):
        raise HTTPException(400, detail=data)

    updated = _load_tokens()
    updated.update(
        {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", refresh_token),
            "account": data.get("account", updated.get("account", "")),
            "country": data.get("country", updated.get("country", "")),
            "expires_in": data.get("expires_in", updated.get("expires_in", 0)),
            "refresh_expires_in": data.get("refresh_expires_in", updated.get("refresh_expires_in", 0)),
            "fetched_at": int(time.time()),
        }
    )
    _save_tokens(updated)

    return {
        "ok": True,
        "account": updated.get("account", ""),
        "country": updated.get("country", ""),
        "access_token_preview": _mask(updated.get("access_token", "")),
        "refresh_token_preview": _mask(updated.get("refresh_token", "")),
    }


@router.get("/bootstrap-refresh")
async def bootstrap_refresh():
    """One-click browser endpoint: refresh token, with auth_code fallback for first bootstrap."""
    _must_have_config()

    saved = _load_tokens()
    refresh_token = str(saved.get("refresh_token") or "").strip()
    env_code = _env_auth_code()

    try:
        if refresh_token:
            data = await _token_refresh(refresh_token)
            mode = "refresh_token"
        elif env_code:
            data = await _token_create(env_code)
            mode = "auth_code"
        else:
            return _error_page(
                "Lazada Bootstrap Failed",
                "No refresh token found and LAZADA_AUTH_CODE is not set.",
                400,
            )
    except Exception:
        log.exception("[Lazada OAuth] bootstrap token request failed")
        return _error_page("Lazada Bootstrap Error", "Unable to complete bootstrap token request.", 500)

    if str(data.get("code", "0")) != "0" or not data.get("access_token"):
        log.warning("[Lazada OAuth] bootstrap rejected code=%s mode=%s", data.get("code"), mode)
        return _error_page("Lazada Bootstrap Failed", "Lazada rejected bootstrap token request.", 400)

    updated = _load_tokens()
    updated.update(
        {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", refresh_token),
            "account": data.get("account", updated.get("account", "")),
            "country": data.get("country", updated.get("country", "")),
            "expires_in": data.get("expires_in", updated.get("expires_in", 0)),
            "refresh_expires_in": data.get("refresh_expires_in", updated.get("refresh_expires_in", 0)),
            "fetched_at": int(time.time()),
        }
    )
    _save_tokens(updated)

    return HTMLResponse(
        f"""
        <html><body style="font-family:monospace;padding:40px;background:#0d0f12;color:#c8cdd8">
        <h2 style="color:#22c55e">Lazada Bootstrap Success</h2>
        <p><b>Mode:</b> {mode}</p>
        <p><b>Account:</b> {updated.get('account') or '(unknown)'}</p>
        <p><b>Country:</b> {updated.get('country') or '(unknown)'}</p>
        <p><b>Access token:</b> {_mask(updated.get('access_token', ''))}</p>
        <p><b>Refresh token:</b> {_mask(updated.get('refresh_token', ''))}</p>
        <p style="color:#f59e0b">Token saved. You can close this tab.</p>
        </body></html>
        """
    )