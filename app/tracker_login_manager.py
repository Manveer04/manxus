"""Dedicated login manager for Shopee tracker sessions.

This opens a headed Chromium browser on the virtual display so the user can
sign in once through noVNC, then save cookies to shopee_cookies.json for reuse
by the automated tracker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

os.environ.setdefault("DISPLAY", ":99")

log = logging.getLogger("app.tracker_login")

TRACKER_LOGIN_URL = os.getenv("SHOPEE_TRACKER_LOGIN_URL", "https://shopee.com.my/buyer/login")
TRACKER_COOKIES_FILE = Path(os.getenv("SHOPEE_TRACKER_COOKIES_FILE", "shopee_cookies.json"))
if not TRACKER_COOKIES_FILE.is_absolute():
    TRACKER_COOKIES_FILE = Path(__file__).resolve().parents[2] / TRACKER_COOKIES_FILE


class TrackerLoginManager:
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            if self.page:
                await self._close()

            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-MY",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            self.page = await self.context.new_page()
            await self.page.goto(TRACKER_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            log.info("Tracker login browser opened at %s", TRACKER_LOGIN_URL)

    async def save_session(self) -> str:
        async with self._lock:
            if not self.context:
                raise RuntimeError("No active tracker login session")

            cookies = await self.context.cookies()
            TRACKER_COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
            TRACKER_COOKIES_FILE.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                os.chmod(TRACKER_COOKIES_FILE, 0o600)
            except Exception:
                pass
            await self._close()
            return str(TRACKER_COOKIES_FILE)

    async def cancel(self):
        async with self._lock:
            await self._close()

    async def _close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    @property
    def is_busy(self) -> bool:
        return self.page is not None


tracker_login_manager = TrackerLoginManager()