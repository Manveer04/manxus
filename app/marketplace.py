from __future__ import annotations

import os

from fastapi import HTTPException


SUPPORTED_MARKETPLACES = ("shopee", "shopee_sg", "lazada", "tiktok")


def marketplace_unavailable(operation: str, platform: str | None = None) -> HTTPException:
    detail = "This marketplace operation requires an API/token integration that is not configured in this build."
    if platform:
        detail = f"{detail} ({platform}: {operation})"
    else:
        detail = f"{detail} ({operation})"
    return HTTPException(status_code=503, detail=detail)


def is_marketplace_configured(platform: str) -> bool:
    platform = (platform or "").strip().lower()
    if platform in {"shopee", "shopee_sg"}:
        return bool((os.getenv("SHOPEE_APP_KEY") or "").strip() and (os.getenv("SHOPEE_APP_SECRET") or "").strip())
    if platform == "lazada":
        return bool((os.getenv("LAZADA_APP_KEY") or "").strip() and (os.getenv("LAZADA_APP_SECRET") or "").strip())
    if platform == "tiktok":
        return bool((os.getenv("TIKTOK_APP_KEY") or "").strip() and (os.getenv("TIKTOK_APP_SECRET") or "").strip())
    return False


class TikTokAwbStateConflictError(RuntimeError):
    def __init__(self, code: str = "UNAVAILABLE", message: str = "This marketplace operation requires an API/token integration that is not configured in this build."):
        super().__init__(message)
        self.code = code
        self.message = message

