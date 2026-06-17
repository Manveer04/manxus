from __future__ import annotations

from ._disabled import disabled_sync
from .base import DisabledScraperBase


SHOPS: dict[str, dict[str, object]] = {}


def _load_token(shop_id):
    del shop_id
    return None


def _refresh_token(shop_id):
    return disabled_sync("Shopee token refresh", shop_id)


def shopee_arrange_shipment(*args, **kwargs):
    return disabled_sync("Shopee shipment", *args, **kwargs)


def shopee_create_awb(*args, **kwargs):
    return disabled_sync("Shopee AWB", *args, **kwargs)


def shopee_get_awb_result(*args, **kwargs):
    return disabled_sync("Shopee AWB", *args, **kwargs)


def shopee_get_awb_parameter(*args, **kwargs):
    return disabled_sync("Shopee AWB", *args, **kwargs)


def shopee_get_shipping_parameter(*args, **kwargs):
    return disabled_sync("Shopee shipment", *args, **kwargs)


def shopee_get_tracking_number(*args, **kwargs):
    return disabled_sync("Shopee tracking", *args, **kwargs)


class ShopeeAPIClient:
    def __init__(self, shop_id):
        self.shop_id = shop_id

    def get_order_detail(self, *args, **kwargs):
        return disabled_sync("Shopee order detail lookup", *args, **kwargs)


class ShopeeScraper(DisabledScraperBase):
    platform_name = "shopee"
