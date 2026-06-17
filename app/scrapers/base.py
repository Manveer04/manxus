from __future__ import annotations

import os
from pathlib import Path

from ._disabled import disabled_async, disabled_sync


SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "/app/sessions"))
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


class DisabledScraperBase:
    platform_name = "integration"
    LOGIN_URL = ""

    def __init__(self):
        self.page = None
        self.access_token = ""
        self.refresh_token = ""
        self.app_secret = os.getenv("APP_SECRET", "")
        self.last_update_meta = {}
        self.session_file = SESSIONS_DIR / f"{self.platform_name}.json"
        self.token_session_file = self.session_file

    async def start(self, *args, **kwargs):
        return await disabled_async(f"{self.platform_name} scraper start", *args, **kwargs)

    async def close(self, *args, **kwargs):
        del args, kwargs
        return None

    async def save_session(self, *args, **kwargs):
        return await disabled_async(f"{self.platform_name} scraper session save", *args, **kwargs)

    async def is_logged_in(self, *args, **kwargs):
        return await disabled_async(f"{self.platform_name} scraper login check", *args, **kwargs)

    async def get_inventory(self, *args, **kwargs):
        return await disabled_async(f"{self.platform_name} scraper inventory sync", *args, **kwargs)

    def _base_params(self, *args, **kwargs):
        return disabled_sync(f"{self.platform_name} API client", *args, **kwargs)

    def get_order_sku_sync(self, *args, **kwargs):
        return disabled_sync(f"{self.platform_name} order lookup", *args, **kwargs)

    async def _refresh_access_token(self, *args, **kwargs):
        return await disabled_async(f"{self.platform_name} token refresh", *args, **kwargs)
