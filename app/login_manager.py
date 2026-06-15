"""
Manages headed browser sessions for the web-based login flow.
One login session at a time — opens a real browser on the virtual display,
user logs in via noVNC, then we save the session cookies.
"""
import os
import asyncio
import logging
from typing import Optional

try:
    from app.scrapers import SCRAPERS
except Exception:
    SCRAPERS = {}

os.environ.setdefault("DISPLAY", ":99")
log = logging.getLogger("app.login")


class LoginManager:
    def __init__(self):
        self.active_scraper: Optional[object] = None
        self.active_platform: Optional[str] = None
        self._lock = asyncio.Lock()

    async def start_login(self, platform: str):
        async with self._lock:
            # Close any existing session first
            if self.active_scraper:
                await self._close()

            ScraperClass = SCRAPERS.get(platform)
            if not ScraperClass:
                raise ValueError(f"Unknown platform: {platform}")

            scraper = ScraperClass()
            try:
                # headless=False so it appears on the virtual display (noVNC)
                await scraper.start(headless=False)
                if not scraper.page:
                    raise RuntimeError("Browser page was not initialized")

                if scraper.LOGIN_URL:
                    await scraper.page.goto(scraper.LOGIN_URL, wait_until="domcontentloaded")

                self.active_scraper = scraper
                self.active_platform = platform
                print(f"[Login] Headed browser opened for {platform}")
            except Exception as e:
                try:
                    await scraper.close()
                except Exception:
                    pass
                log.exception("[Login] Failed to start headed login for %s", platform)
                raise RuntimeError(f"Unable to open login browser for {platform}: {e}") from e

    async def save_session(self) -> str:
        async with self._lock:
            if not self.active_scraper:
                raise RuntimeError("No active login session")
            await self.active_scraper.save_session()
            platform = self.active_platform
            print(f"[Login] Session saved for {platform}")
            await self._close()
            return platform

    async def cancel(self):
        async with self._lock:
            await self._close()

    async def _close(self):
        if self.active_scraper:
            try:
                await self.active_scraper.close()
            except Exception:
                pass
            self.active_scraper = None
            self.active_platform = None

    @property
    def is_busy(self) -> bool:
        return self.active_scraper is not None

    @property
    def current_platform(self) -> Optional[str]:
        return self.active_platform


login_manager = LoginManager()
