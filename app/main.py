import asyncio
import base64
from collections import defaultdict, deque
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_db, SessionLocal
from app.api.routes import router
from app.models import DeviceToken
from app.scheduler import start_scheduler, stop_scheduler
from app.email_watcher import run_email_watcher

from app.financials.models import (
    FinancialTransaction,
    PurchaseBatch,
    GeneratedInvoice,
    InvoiceLineItem,
    GeneratedPurchaseOrder,
    PurchaseOrderLineItem,
    PackagingSupplierCategory,
    PackagingSupplierContact,
    PackagingPurchase,
)  # so Alembic sees them
from app.financials.routes import router as fin_router
from app.financials.document_routes import document_router
from app.shopee_auth import router as shopee_auth_router
from app.lazada_auth import router as lazada_auth_router

log = logging.getLogger("app.main")

_api_failure_events: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=200))
_api_request_events: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=2000))
_device_login_attempts: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=100))
_SESSION_COOKIE_NAME = "inventory_sync_session"
_SESSION_MAX_AGE_SECONDS = 60 * 60 * 12
_DEVICE_COOKIE_NAME = "device_token"
_DEVICE_MIN_TOKEN_BYTES = 32
_DEVICE_INACTIVITY_DAYS = 90
_DEVICE_LOGIN_RATE_LIMIT = 10
_DEVICE_LOGIN_RATE_WINDOW_SECONDS = 60
_API_RATE_LIMIT_WINDOW_SECONDS = 60
_API_RATE_LIMIT_MAX_REQUESTS = 300
_API_RATE_LIMIT_BURST_WINDOW_SECONDS = 5
_API_RATE_LIMIT_BURST_MAX_REQUESTS = 80


def _enforce_https() -> bool:
    return (os.getenv("ENFORCE_HTTPS", "false") or "false").strip().lower() == "true"


def _request_is_https(request: Request) -> bool:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _get_configured_auth_credentials() -> tuple[str, str]:
    user = (os.getenv("APP_BASIC_AUTH_USER") or "").strip()
    password = (os.getenv("APP_BASIC_AUTH_PASSWORD") or "").strip()
    return user, password


def _auth_credentials_configured() -> bool:
    user, password = _get_configured_auth_credentials()
    return bool(user and password)


def _get_session_signing_secret() -> str:
    # Use an explicit secret when available; otherwise fall back to the auth password.
    secret = (os.getenv("APP_AUTH_SESSION_SECRET") or "").strip()
    if secret:
        return secret
    _, password = _get_configured_auth_credentials()
    return password


def _sign_session_payload(encoded_payload: str) -> str:
    secret = _get_session_signing_secret()
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), encoded_payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _encode_session_token(username: str) -> str:
    payload = {"u": username, "exp": int(time.time()) + _SESSION_MAX_AGE_SECONDS}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = _sign_session_payload(encoded)
    if not signature:
        return ""
    return f"{encoded}.{signature}"


def _decode_session_token(token: str) -> dict[str, object] | None:
    encoded_payload, sep, supplied_sig = token.partition(".")
    if not sep or not encoded_payload or not supplied_sig:
        return None

    expected_sig = _sign_session_payload(encoded_payload)
    if not expected_sig or not secrets.compare_digest(supplied_sig, expected_sig):
        return None

    padded = encoded_payload + ("=" * (-len(encoded_payload) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def _is_session_authorized(request: Request) -> bool:
    del request
    return True


def _is_protected_ui_path(path: str) -> bool:
    return path in {"/", "/financials", "/purchases", "/documents", "/orders-admin", "/my-devices"} or path.startswith("/login/")


def _utcnow() -> datetime:
    return datetime.utcnow()


def _device_expires_at(from_dt: datetime | None = None) -> datetime:
    base = from_dt or _utcnow()
    return base + timedelta(days=_DEVICE_INACTIVITY_DAYS)


def _hash_device_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _decode_base64url_token(raw_token: str) -> bytes | None:
    token = raw_token.strip()
    if not token:
        return None
    try:
        padded = token + ("=" * (-len(token) % 4))
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except Exception:
        return None


def _token_minimum_bytes(raw_token: str) -> int:
    base64_bytes = _decode_base64url_token(raw_token)
    if base64_bytes is not None:
        return len(base64_bytes)

    try:
        if len(raw_token) % 2 == 0:
            return len(bytes.fromhex(raw_token))
    except Exception:
        pass

    return len(raw_token.encode("utf-8"))


def _extract_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _device_login_is_rate_limited(client_ip: str) -> bool:
    now = time.time()
    attempts = _device_login_attempts[client_ip]
    cutoff = now - _DEVICE_LOGIN_RATE_WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()

    if len(attempts) >= _DEVICE_LOGIN_RATE_LIMIT:
        return True

    attempts.append(now)
    return False


def _set_auth_session_cookie(response: JSONResponse, request: Request, session_token: str) -> None:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=session_token,
        max_age=_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https" or forwarded_proto == "https",
        path="/",
    )


def _set_device_cookie(response: JSONResponse, request: Request, raw_token: str) -> None:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    response.set_cookie(
        key=_DEVICE_COOKIE_NAME,
        value=raw_token,
        max_age=_DEVICE_INACTIVITY_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https" or forwarded_proto == "https",
        path="/",
    )


def _log_device_login_attempt(client_ip: str, ok: bool, reason: str) -> None:
    now_iso = _utcnow().isoformat(timespec="seconds")
    status = "ok" if ok else "denied"
    log.info("[DeviceAuth] login_attempt status=%s ip=%s ts=%s reason=%s", status, client_ip, now_iso, reason)


def _get_session_user(request: Request) -> str | None:
    if not _auth_credentials_configured():
        return "public"

    token = (request.cookies.get(_SESSION_COOKIE_NAME) or "").strip()
    if not token:
        return None

    payload = _decode_session_token(token)
    if not payload:
        return None

    username = str(payload.get("u") or "").strip()
    if not username:
        return None

    try:
        expires_at = int(payload.get("exp") or 0)
    except Exception:
        return None

    if expires_at <= int(time.time()):
        return None

    configured_user, _ = _get_configured_auth_credentials()
    if configured_user and not secrets.compare_digest(username, configured_user):
        return None

    return username


def _sanitize_next_path(candidate: str) -> str:
    if not candidate.startswith("/"):
        return "/"
    if candidate.startswith("//"):
        return "/"
    return candidate


def _is_authorized(request: Request) -> bool:
    if not _auth_credentials_configured():
        return True
    return _get_session_user(request) is not None


def _is_api_auth_required() -> bool:
    return (os.getenv("REQUIRE_API_AUTH", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}


def _get_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        if value < minimum:
            return minimum
        return value
    except Exception:
        return default


def _api_is_rate_limited(client_ip: str) -> tuple[bool, int]:
    now = time.time()
    events = _api_request_events[client_ip]

    cutoff = now - _API_RATE_LIMIT_WINDOW_SECONDS
    while events and events[0] < cutoff:
        events.popleft()

    if len(events) >= _API_RATE_LIMIT_MAX_REQUESTS:
        retry_after = max(1, int(_API_RATE_LIMIT_WINDOW_SECONDS - (now - events[0])))
        return True, retry_after

    burst_cutoff = now - _API_RATE_LIMIT_BURST_WINDOW_SECONDS
    burst_events = [ts for ts in events if ts >= burst_cutoff]
    if len(burst_events) >= _API_RATE_LIMIT_BURST_MAX_REQUESTS:
        retry_after = max(1, int(_API_RATE_LIMIT_BURST_WINDOW_SECONDS - (now - burst_events[0])))
        return True, retry_after

    events.append(now)
    return False, 0


def _record_api_failure(client_ip: str, status_code: int) -> None:
    now = time.time()
    if status_code >= 400:
        _api_failure_events[client_ip].append(now)

    recent = _api_failure_events.get(client_ip)
    if not recent:
        return

    cutoff = now - 300
    while recent and recent[0] < cutoff:
        recent.popleft()

    if len(recent) >= 20:
        log.warning("[Security] Suspicious traffic from %s: %d API failures in 5m", client_ip, len(recent))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()

    async def _start_watcher():
        await asyncio.sleep(5)
        log.info("Starting email watcher task")
        await run_email_watcher(SessionLocal)

    watcher_task = asyncio.create_task(_start_watcher())

    def _watcher_done(task: asyncio.Task):
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            log.info("Email watcher task cancelled")
            return
        if exc:
            log.exception("Email watcher task crashed", exc_info=exc)

    watcher_task.add_done_callback(_watcher_done)
    yield
    stop_scheduler()


app = FastAPI(title="Inventory Sync", lifespan=lifespan)
app.include_router(router)
app.include_router(fin_router, prefix="/api/financials", tags=["financials"])
app.include_router(document_router, prefix="/api/financials", tags=["documents"])
app.include_router(shopee_auth_router, prefix="/api/shopee")
app.include_router(lazada_auth_router, prefix="/api/lazada")


@app.middleware("http")
async def add_cache_control_headers(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path

    if _enforce_https() and not _request_is_https(request):
        return JSONResponse(status_code=426, content={"detail": "HTTPS is required"})

    if path.startswith("/api/"):
        limited, retry_after = _api_is_rate_limited(client_ip)
        if limited:
            log.warning("[Security] Rate limit triggered ip=%s path=%s retry_after=%s", client_ip, path, retry_after)
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please retry shortly."},
            )
            response.headers["Retry-After"] = str(retry_after)
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

    if request.url.path.startswith("/api/") and _is_api_auth_required():
        user, password = _get_configured_auth_credentials()
        if not user or not password:
            return JSONResponse(
                status_code=503,
                content={"detail": "API authentication is required but credentials are not configured"},
            )

    if _is_api_auth_required() and _is_protected_ui_path(request.url.path) and not _is_authorized(request):
        login_next = quote(request.url.path, safe="/")
        return RedirectResponse(url=f"/auth/login?next={login_next}", status_code=303)

    started_at = time.perf_counter()

    if request.url.path.startswith("/api/") and not _is_authorized(request):
        _record_api_failure(client_ip, 401)
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )

    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        if path.startswith("/api/"):
            log.exception("[API] unhandled exception ip=%s method=%s path=%s elapsed_ms=%s", client_ip, request.method, path, elapsed_ms)
        raise

    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)

    if path.startswith("/api/"):
        if response.status_code >= 500:
            log.error("[API] error ip=%s method=%s path=%s status=%s elapsed_ms=%s", client_ip, request.method, path, response.status_code, elapsed_ms)
        elif response.status_code >= 400:
            log.warning("[API] client_error ip=%s method=%s path=%s status=%s elapsed_ms=%s", client_ip, request.method, path, response.status_code, elapsed_ms)
            _record_api_failure(client_ip, response.status_code)
        else:
            log.info("[API] request ip=%s method=%s path=%s status=%s elapsed_ms=%s", client_ip, request.method, path, response.status_code, elapsed_ms)

        if len(request.url.query) > 1024:
            log.warning("[Security] long query string from %s path=%s len=%d", client_ip, path, len(request.url.query))

    # Cloud tunnels and browsers may cache GET API responses; force revalidation.
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    # Ensure browsers pick up new service worker scripts quickly.
    if path == "/service-worker.js":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    return response

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_dashboard():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/auth/login")
async def serve_auth_login(request: Request):
    if _is_authorized(request):
        next_path = _sanitize_next_path((request.query_params.get("next") or "/").strip())
        return RedirectResponse(url=next_path, status_code=303)
    return FileResponse(str(STATIC_DIR / "auth_login.html"))


@app.post("/auth/login")
async def handle_auth_login(request: Request):
    user, password = _get_configured_auth_credentials()
    if not user or not password:
        return JSONResponse(status_code=503, content={"detail": "Authentication credentials are not configured"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid request payload"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Invalid request payload"})

    supplied_user = str(body.get("username") or "").strip()
    supplied_password = str(body.get("password") or "")
    next_path = _sanitize_next_path(str(body.get("next") or "/").strip() or "/")

    if not (
        secrets.compare_digest(supplied_user, user)
        and secrets.compare_digest(supplied_password, password)
    ):
        return JSONResponse(status_code=401, content={"detail": "Invalid username or password"})

    token = _encode_session_token(user)
    if not token:
        return JSONResponse(status_code=500, content={"detail": "Session configuration error"})

    response = JSONResponse(content={"ok": True, "redirect": next_path})
    _set_auth_session_cookie(response, request, token)
    return response


@app.get("/auth/device/status")
async def get_device_status(request: Request):
    user = _get_session_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    raw_token = request.cookies.get(_DEVICE_COOKIE_NAME, "")
    if not raw_token:
        return JSONResponse(content={"enabled": False})

    token_hash = _hash_device_token(raw_token)
    now = _utcnow()
    with SessionLocal() as db:
        token_record = db.query(DeviceToken).filter(DeviceToken.token_hash == token_hash).first()
        if not token_record:
            return JSONResponse(content={"enabled": False})

        if not secrets.compare_digest(token_hash, token_record.token_hash):
            return JSONResponse(content={"enabled": False})

        if not secrets.compare_digest(str(token_record.user_id), user):
            return JSONResponse(content={"enabled": False})

        if token_record.expires_at <= now:
            return JSONResponse(content={"enabled": False})

    return JSONResponse(content={"enabled": True, "device_label": token_record.device_label, "expires_at": token_record.expires_at.isoformat()})


@app.post("/auth/device/register")
async def register_device(request: Request):
    user = _get_session_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid request payload"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Invalid request payload"})

    raw_token = str(body.get("token") or "").strip()
    if not raw_token:
        return JSONResponse(status_code=400, content={"detail": "token is required"})

    token_size = _token_minimum_bytes(raw_token)
    if token_size < _DEVICE_MIN_TOKEN_BYTES:
        return JSONResponse(
            status_code=400,
            content={"detail": f"token must contain at least {_DEVICE_MIN_TOKEN_BYTES} bytes of randomness"},
        )

    device_label_raw = str(body.get("device_label") or "").strip()
    device_label = device_label_raw[:120] if device_label_raw else None

    token_hash = _hash_device_token(raw_token)
    now = _utcnow()
    expires_at = _device_expires_at(now)
    with SessionLocal() as db:
        token_record = db.query(DeviceToken).filter(DeviceToken.token_hash == token_hash).first()
        if token_record and secrets.compare_digest(str(token_record.user_id), user):
            token_record.device_label = device_label
            token_record.last_used_at = now
            token_record.expires_at = expires_at
        else:
            db.add(
                DeviceToken(
                    user_id=user,
                    token_hash=token_hash,
                    device_label=device_label,
                    last_used_at=now,
                    expires_at=expires_at,
                )
            )
        db.commit()

    response = JSONResponse(
        content={
            "ok": True,
            "device_label": device_label,
            "expires_at": expires_at.isoformat(),
        }
    )
    _set_device_cookie(response, request, raw_token)
    return response


@app.get("/auth/device/bootstrap")
async def device_auto_login(request: Request):
    client_ip = _extract_client_ip(request)
    if _device_login_is_rate_limited(client_ip):
        _log_device_login_attempt(client_ip, False, "rate_limited")
        return JSONResponse(status_code=429, content={"detail": "Too many attempts"})

    raw_token = request.cookies.get(_DEVICE_COOKIE_NAME, "")
    if not raw_token:
        _log_device_login_attempt(client_ip, False, "missing_cookie")
        return JSONResponse(status_code=401, content={"detail": "Device token missing"})

    token_hash = _hash_device_token(raw_token)
    now = _utcnow()

    with SessionLocal() as db:
        token_record = db.query(DeviceToken).filter(DeviceToken.token_hash == token_hash).first()
        if not token_record:
            _log_device_login_attempt(client_ip, False, "token_not_found")
            return JSONResponse(status_code=401, content={"detail": "Device token invalid or expired"})

        if not secrets.compare_digest(token_hash, token_record.token_hash):
            _log_device_login_attempt(client_ip, False, "hash_mismatch")
            return JSONResponse(status_code=401, content={"detail": "Device token invalid or expired"})

        if token_record.expires_at <= now:
            db.delete(token_record)
            db.commit()
            _log_device_login_attempt(client_ip, False, "token_expired")
            return JSONResponse(status_code=401, content={"detail": "Device token invalid or expired"})

        token_record.last_used_at = now
        token_record.expires_at = _device_expires_at(now)
        db.commit()
        user_id = str(token_record.user_id)

    user, _ = _get_configured_auth_credentials()
    if not user or not secrets.compare_digest(user_id, user):
        _log_device_login_attempt(client_ip, False, "user_mismatch")
        return JSONResponse(status_code=401, content={"detail": "Device token invalid or expired"})

    session_token = _encode_session_token(user_id)
    if not session_token:
        _log_device_login_attempt(client_ip, False, "session_error")
        return JSONResponse(status_code=500, content={"detail": "Session configuration error"})

    response = JSONResponse(content={"ok": True, "redirect": "/"})
    _set_auth_session_cookie(response, request, session_token)
    _log_device_login_attempt(client_ip, True, "authenticated")
    return response


@app.get("/auth/device/my-devices")
async def list_my_devices(request: Request):
    user = _get_session_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    with SessionLocal() as db:
        records = (
            db.query(DeviceToken)
            .filter(DeviceToken.user_id == user)
            .order_by(DeviceToken.last_used_at.desc())
            .all()
        )

    return JSONResponse(
        content={
            "devices": [
                {
                    "id": r.id,
                    "device_label": r.device_label,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                }
                for r in records
            ]
        }
    )


@app.delete("/auth/device/{device_id}")
async def revoke_device(device_id: int, request: Request):
    user = _get_session_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    with SessionLocal() as db:
        record = (
            db.query(DeviceToken)
            .filter(DeviceToken.id == device_id)
            .filter(DeviceToken.user_id == user)
            .first()
        )
        if not record:
            return JSONResponse(status_code=404, content={"detail": "Device not found"})

        db.delete(record)
        db.commit()

    return JSONResponse(content={"ok": True})


@app.post("/auth/logout")
async def handle_auth_logout():
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key=_SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/login/{platform}")
async def serve_login(platform: str):
    return FileResponse(str(STATIC_DIR / "login.html"))

@app.get("/financials")
async def serve_financials():
    return FileResponse(str(STATIC_DIR / "financials.html"))

@app.get("/purchases")
async def serve_purchases():
    return FileResponse(str(STATIC_DIR / "purchases.html"))

@app.get("/documents")
async def serve_documents():
    return FileResponse(str(STATIC_DIR / "documents.html"))

@app.get("/shopee-tracker")
async def serve_shopee_tracker():
    return FileResponse(str(STATIC_DIR / "shopee_tracker.html"))

@app.get("/orders-admin")
async def serve_orders_admin():
    return FileResponse(str(STATIC_DIR / "orders_admin.html"))


@app.get("/my-devices")
async def serve_my_devices():
    return FileResponse(str(STATIC_DIR / "my_devices.html"))

@app.get("/manifest.json")
async def serve_manifest():
    return FileResponse(str(STATIC_DIR / "manifest.json"), media_type="application/manifest+json")

@app.get("/service-worker.js")
async def serve_service_worker():
    return FileResponse(str(STATIC_DIR / "service-worker.js"), media_type="application/javascript")