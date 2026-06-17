from __future__ import annotations

from ._disabled import disabled_sync
from .base import DisabledScraperBase


def lazada_arrange_shipment(*args, **kwargs):
    return disabled_sync("Lazada shipment", *args, **kwargs)


def lazada_create_awb(*args, **kwargs):
    return disabled_sync("Lazada AWB", *args, **kwargs)


def lazada_get_awb_result(*args, **kwargs):
    return disabled_sync("Lazada AWB", *args, **kwargs)


class LazadaScraper(DisabledScraperBase):
    platform_name = "lazada"
