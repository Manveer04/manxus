"""
Shopee OAuth routes — mount in main.py:
    from app.shopee_auth import router as shopee_auth_router
    app.include_router(shopee_auth_router, prefix="/api/shopee")

Handles:
  - GET  /api/shopee/auth-url          → generate authorization URL
  - GET  /api/shopee/callback          → catch redirect, exchange code for tokens
  - POST /api/shopee/refresh/{shop_id} → manually refresh a token
  - GET  /api/shopee/status            → show token status for all shops
"""
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.security_utils import make_oauth_state, secure_json_load, secure_json_save, verify_oauth_state

router = APIRouter()
log = logging.getLogger("app.shopee_auth")

# ── Config ────────────────────────────────────────────────────────────────────
PARTNER_ID  = int(os.getenv("SHOPEE_APP_KEY", "0"))
PARTNER_KEY = os.getenv("SHOPEE_APP_SECRET", "")
HOST        = "https://partner.shopeemobile.com"
REDIRECT    = os.getenv("SHOPEE_REDIRECT_URL", "https://192.168.50.129:8080/api/shopee/callback")

# In-memory token store (also saved to DB via ShopeeToken model below)
_token_cache: dict = {}


def _validate_https_redirect(redirect: str) -> None:
    parsed = urlparse(redirect)
    allow_insecure = (os.getenv("ALLOW_INSECURE_REDIRECT_URI", "false") or "false").strip().lower() == "true"
    if parsed.scheme == "https" and parsed.netloc:
        return
    if allow_insecure:
        return
    raise HTTPException(500, "SHOPEE_REDIRECT_URL must use https in secure mode")


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


# ── Signing ───────────────────────────────────────────────────────────────────
def _sign(path: str, access_token: str = "", shop_id: int = 0) -> tuple[int, str]:
    ts   = int(time.time())
    if shop_id:
        base = f"{PARTNER_ID}{path}{ts}{access_token}{shop_id}"
    else:
        base = f"{PARTNER_ID}{path}{ts}"
    sign = hmac.new(PARTNER_KEY.encode(), base.encode(), hashlib.sha256).hexdigest()
    return ts, sign


def _base_params(path: str, access_token: str = "", shop_id: int = 0) -> dict:
    ts, sign = _sign(path, access_token, shop_id)
    p = {"partner_id": PARTNER_ID, "timestamp": ts, "sign": sign}
    if shop_id:
        p["shop_id"]      = shop_id
        p["access_token"] = access_token
    return p


# ── Token DB helpers ──────────────────────────────────────────────────────────
def _save_token(shop_id: int, shop_name: str, data: dict):
    """Persist token to encrypted-at-rest JSON file per shop."""
    import pathlib
    token_dir = pathlib.Path("/app/data/shopee_tokens")
    token_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "shop_id":       shop_id,
        "shop_name":     shop_name,
        "access_token":  data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "expire_in":     data.get("expire_in", 14400),
        "fetched_at":    int(time.time()),
    }
    token_file = token_dir / f"{shop_id}.json"
    secure_json_save(token_file, record, secret_hint=PARTNER_KEY)
    _token_cache[shop_id] = record
    print(f"[Shopee Auth] Saved token for shop {shop_id} ({shop_name})")
    return record


def _load_token(shop_id: int) -> dict | None:
    if shop_id in _token_cache:
        return _token_cache[shop_id]
    import pathlib
    path = pathlib.Path(f"/app/data/shopee_tokens/{shop_id}.json")
    if path.exists():
        record = secure_json_load(path, secret_hint=PARTNER_KEY)
        if record:
            _token_cache[shop_id] = record
            return record
    return None


def _load_all_tokens() -> list[dict]:
    import pathlib
    token_dir = pathlib.Path("/app/data/shopee_tokens")
    if not token_dir.exists():
        return []
    tokens = []
    for f in token_dir.glob("*.json"):
        record = secure_json_load(f, secret_hint=PARTNER_KEY)
        if record:
            tokens.append(record)
    return tokens


def get_valid_token(shop_id: int) -> str:
    """Return a valid access token, refreshing if needed. Raises if unavailable."""
    record = _load_token(shop_id)
    if not record:
        raise ValueError(f"No token found for shop_id {shop_id}. Please authorize first.")

    age     = int(time.time()) - record["fetched_at"]
    expires = record["expire_in"]

    # Refresh if less than 30 minutes remaining
    if age >= expires - 1800:
        print(f"[Shopee Auth] Token for {shop_id} expiring soon — refreshing")
        record = _do_refresh(shop_id, record["refresh_token"], record.get("shop_name", ""))

    return record["access_token"]


def _do_refresh(shop_id: int, refresh_token: str, shop_name: str = "") -> dict:
    PATH = "/api/v2/auth/access_token/get"
    params = _base_params(PATH)
    resp = httpx.post(
        f"{HOST}{PATH}",
        params=params,
        json={"shop_id": shop_id, "refresh_token": refresh_token, "partner_id": PARTNER_ID},
        timeout=15,
    )
    data = resp.json()
    if not data.get("access_token"):
        raise ValueError(f"Token refresh failed: {data}")
    return _save_token(shop_id, shop_name, data)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/auth-url")
def get_auth_url(shop: str = "main"):
    """Generate Shopee authorization URL. Open in browser to authorize."""
    _validate_https_redirect(REDIRECT)
    PATH  = "/api/v2/shop/auth_partner"
    ts, sign = _sign(PATH)
    signed_state = make_oauth_state(
        secret_hint=PARTNER_KEY,
        provider="shopee",
        context={"shop": (shop or "main")[:40]},
    )
    url = (
        f"{HOST}{PATH}"
        f"?partner_id={PARTNER_ID}&timestamp={ts}&sign={sign}"
        f"&redirect={REDIRECT}"
        f"&state={signed_state}"
    )
    return {
        "shop":  shop,
        "url":   url,
        "state": signed_state,
        "note":  "Open this URL in your browser and log in with the correct Shopee seller account",
    }


@router.get("/callback")
async def oauth_callback(code: str = "", shop_id: int = 0, error: str = "", state: str = ""):
    """Shopee redirects here after seller authorizes. Exchanges code for tokens."""
    _validate_https_redirect(REDIRECT)
    if error or not code or not shop_id:
        return _error_page("Authorization Failed", "Authorization was rejected or missing required fields.", 400)

    state_ok, _ = verify_oauth_state(state, secret_hint=PARTNER_KEY, provider="shopee")
    if not state_ok:
        return _error_page("Authorization Failed", "Invalid or expired OAuth state. Please start login again.", 400)

    PATH = "/api/v2/auth/token/get"
    params = _base_params(PATH)

    try:
        resp = httpx.post(
            f"{HOST}{PATH}",
            params=params,
            json={"code": code, "shop_id": shop_id, "partner_id": PARTNER_ID},
            timeout=15,
        )
        data = resp.json()

        if not data.get("access_token"):
            log.warning("[Shopee OAuth] token exchange rejected for shop_id=%s", shop_id)
            return _error_page("Token Exchange Failed", "Shopee rejected the authorization code.", 400)

        # Get shop info for the name
        shop_name = f"shop_{shop_id}"
        try:
            INFO_PATH = "/api/v2/shop/get_shop_info"
            access_token = data["access_token"]
            ip = _base_params(INFO_PATH, access_token, shop_id)
            ir = httpx.get(f"{HOST}{INFO_PATH}", params=ip, timeout=10)
            shop_name = ir.json().get("response", {}).get("shop_name", shop_name)
        except Exception:
            pass

        record = _save_token(shop_id, shop_name, data)
        expires_at = datetime.fromtimestamp(record["fetched_at"] + record["expire_in"])

        return HTMLResponse(f"""
        <html><body style="font-family:monospace;padding:40px;background:#0d0f12;color:#c8cdd8">
        <h2 style="color:#22c55e">✓ Authorization Successful</h2>
        <p><b>Shop:</b> {shop_name} (ID: {shop_id})</p>
        <p><b>Access token expires:</b> {expires_at.strftime('%Y-%m-%d %H:%M')}</p>
        <p><b>Refresh token:</b> stored (valid 30 days)</p>
        <br>
        <p style="color:#f59e0b">You can close this tab. Token has been saved.</p>
        <p><a href="/" style="color:#5b66f5">← Back to Dashboard</a></p>
        </body></html>
        """)

    except Exception:
        log.exception("[Shopee OAuth] callback error for shop_id=%s", shop_id)
        return _error_page("Authorization Error", "Unable to complete Shopee authorization flow.", 500)


@router.post("/refresh/{shop_id}")
def refresh_token(shop_id: int):
    """Manually force a token refresh for a shop."""
    record = _load_token(shop_id)
    if not record:
        raise HTTPException(404, f"No token found for shop_id {shop_id}")
    try:
        record = _do_refresh(shop_id, record["refresh_token"], record.get("shop_name", ""))
        return {"status": "ok", "shop_id": shop_id, "shop_name": record["shop_name"]}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/status")
def token_status():
    """Show current token status for all authorized shops."""
    tokens = _load_all_tokens()
    now    = int(time.time())
    return {
        "shops": [
            {
                "shop_id":    t["shop_id"],
                "shop_name":  t.get("shop_name", ""),
                "age_minutes":  round((now - t["fetched_at"]) / 60),
                "expires_in_minutes": max(0, round((t["fetched_at"] + t["expire_in"] - now) / 60)),
                "needs_refresh": (now - t["fetched_at"]) >= t["expire_in"] - 1800,
                "has_refresh_token": bool(t.get("refresh_token")),
            }
            for t in tokens
        ]
    }


@router.post("/bootstrap")
def bootstrap_tokens():
    """
    On first deploy, call this once to save the env-var tokens to disk.
    Safe to call multiple times — only writes if token file doesn't exist.
    """
    import pathlib
    TOKEN_DIR = pathlib.Path("/app/data/shopee_tokens")
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    access  = os.getenv("SHOPEE_ACCESS_TOKEN", "")
    refresh = os.getenv("SHOPEE_REFRESH_TOKEN", "")
    main_id = int(os.getenv("SHOPEE_MAIN_SHOP_ID", "0"))
    sg_id   = int(os.getenv("SHOPEE_SG_SHOP_ID",   "0"))

    if not access or not refresh:
        raise HTTPException(400, "SHOPEE_ACCESS_TOKEN / SHOPEE_REFRESH_TOKEN not set in environment")

    saved = []
    for shop_id, name in [(main_id, "manzillglobe (MY)"), (sg_id, "manzillglobe.sg (SG)")]:
        if not shop_id:
            continue
        path = TOKEN_DIR / f"{shop_id}.json"
        if not path.exists():
            record = _save_token(shop_id, name, {
                "access_token":  access,
                "refresh_token": refresh,
                "expire_in":     14400,
            })
            saved.append({"shop_id": shop_id, "name": name, "action": "created"})
        else:
            saved.append({"shop_id": shop_id, "name": name, "action": "already_exists"})

    return {"status": "ok", "shops": saved}