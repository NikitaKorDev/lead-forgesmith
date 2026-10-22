"""Playwright-based fetching for sites that reject plain HTTP clients.

OLX sits behind a CloudFront WAF that blocks httpx on TLS/header fingerprint
regardless of the User-Agent, while a real Chromium session gets through. The
same browser layer will serve Phase 2 enrichment.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import structlog

from forgesmith.config import Settings
from forgesmith.config import settings as default_settings

log = structlog.get_logger(__name__)

_BLOCKED_RESOURCES = {"image", "media", "font"}


@dataclass(slots=True)
class Page:
    url: str
    status_code: int
    text: str


class BrowserFetcher:
    """Headless Chromium with a persistent context and per-host pacing."""

    def __init__(self, settings: Settings | None = None, *, concurrency: int = 3) -> None:
        self.settings = settings or default_settings
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self._last_hit: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> BrowserFetcher:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - env issue
            raise RuntimeError(
                "Playwright is not installed. Run: uv sync && uv run playwright install chromium"
            ) from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self._context = await self._browser.new_context(
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
            viewport={"width": 1366, "height": 900},
            user_agent=self.settings.user_agent,
            extra_http_headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"},
        )
        self._context.set_default_navigation_timeout(self.settings.request_timeout * 1000)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = self._browser = self._playwright = None

    async def _throttle(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self.settings.request_delay - (time.monotonic() - self._last_hit.get(host, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[host] = time.monotonic()

    async def get(self, url: str, **_kwargs: object) -> Page | None:
        host = url.split("/")[2] if "://" in url else url
        async with self._semaphore:
            await self._throttle(host)
            page = await self._context.new_page()

            async def route_handler(route: Any) -> None:
                if route.request.resource_type in _BLOCKED_RESOURCES:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", route_handler)
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
                html = await page.content()
                status = response.status if response is not None else 0
                log.debug("browser.get", url=url, status=status, bytes=len(html))
                if status >= 400:
                    return None
                return Page(url=page.url, status_code=status, text=html)
            except Exception as exc:
                log.debug("browser.error", url=url, error=str(exc))
                return None
            finally:
                await page.close()

    async def get_json(self, url: str, **kwargs: object) -> object | None:
        import json

        page = await self.get(url, **kwargs)
        if page is None:
            return None
        try:
            return json.loads(page.text)
        except ValueError:
            return None
