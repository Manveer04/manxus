from __future__ import annotations

from ._disabled import disabled_sync
from .base import DisabledScraperBase


class TikTokAwbStateConflictError(RuntimeError):
    pass


def tiktok_arrange_shipment(*args, **kwargs):
    return disabled_sync("TikTok shipment", *args, **kwargs)


def tiktok_create_awb(*args, **kwargs):
    return disabled_sync("TikTok AWB", *args, **kwargs)


def tiktok_get_awb_result(*args, **kwargs):
    return disabled_sync("TikTok AWB", *args, **kwargs)


class TikTokScraper(DisabledScraperBase):
    platform_name = "tiktok"
