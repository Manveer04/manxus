import base64
import json
import secrets
import time
from pathlib import Path
from typing import Any

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padded = value + ("=" * (-len(value) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def secure_json_load(path: Path, secret_hint: str = "") -> dict[str, Any]:
    del secret_hint
    if not path.exists():
        return {}

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    return parsed if isinstance(parsed, dict) else {}


def secure_json_save(path: Path, data: dict[str, Any], secret_hint: str = "") -> None:
    del secret_hint
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_oauth_state(secret_hint: str, provider: str, ttl_seconds: int = 600, context: dict[str, Any] | None = None) -> str:
    del secret_hint
    now = int(time.time())
    payload = {
        "p": provider,
        "iat": now,
        "exp": now + max(60, int(ttl_seconds)),
        "n": secrets.token_urlsafe(18),
        "ctx": context or {},
    }
    return _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def verify_oauth_state(state: str, secret_hint: str, provider: str) -> tuple[bool, dict[str, Any]]:
    del secret_hint
    encoded = (state or "").strip()
    if not encoded:
        return False, {}

    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except Exception:
        return False, {}

    if not isinstance(payload, dict):
        return False, {}
    if str(payload.get("p") or "") != provider:
        return False, {}

    now = int(time.time())
    exp = int(payload.get("exp") or 0)
    if exp < now:
        return False, {}

    ctx = payload.get("ctx")
    return True, ctx if isinstance(ctx, dict) else {}