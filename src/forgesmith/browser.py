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
from forgesmith.enrichment.models import FetchResult
from forgesmith.http import HostPacer, content_type_error, validate_public_url

log = structlog.get_logger(__name__)

_BLOCKED_RESOURCES = {"image", "media", "font"}


def _unsafe_browser_request(url: str) -> bool:
    """Validate real navigations while leaving data:/blob: subresources alone."""
    if not url.startswith(("http://", "https://")):
        return False
    return validate_public_url(url) is not None


@dataclass(slots=True)
class Page:
    url: str
    status_code: int
    text: str


class BrowserFetcher:
    """Headless Chromium with a persistent context and per-host pacing."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        concurrency: int = 3,
        pacer: HostPacer | None = None,
    ) -> None:
        self.settings = settings or default_settings
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self.pacer = pacer or HostPacer(self.settings.request_delay)
        self._start_lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._context is not None

    async def ensure_started(self) -> None:
        """Launch Chromium once; Phase 2 is sequential so this is a simple guard."""
        if self._context is not None:
            return
        async with self._start_lock:
            if self._context is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - env issue
                raise RuntimeError(
                    "Playwright is not installed. "
                    "Run: uv sync && uv run playwright install chromium"
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

    async def __aenter__(self) -> BrowserFetcher:
        await self.ensure_started()
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
        await self.pacer.wait(host)

    async def get(self, url: str, **_kwargs: object) -> Page | None:
        host = url.split("/")[2] if "://" in url else url
        async with self._semaphore:
            await self._throttle(host)
            page = await self._context.new_page()

            async def route_handler(route: Any) -> None:
                if route.request.resource_type in _BLOCKED_RESOURCES:
                    await route.abort()
                elif _unsafe_browser_request(route.request.url):
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

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        request_timeout: float | None = None,
        settle_ms: int | None = None,
    ) -> FetchResult:
        """Fetch a page in Chromium, waiting briefly for JS-hydrated contacts."""
        started = time.monotonic()
        cap = max_bytes or self.settings.enrich_max_bytes
        settle = self.settings.enrich_browser_settle_ms if settle_ms is None else settle_ms
        timeout_ms = int((request_timeout or self.settings.request_timeout) * 1000)
        unsafe = validate_public_url(url)
        if unsafe is not None:
            return FetchResult(
                url=url,
                final_url=url,
                status=None,
                html="",
                engine="browser",
                error=f"unsafe_url:{unsafe}",
            )

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            await self.ensure_started()
        except Exception as exc:  # pragma: no cover - env issue
            log.debug("browser.unavailable", error=type(exc).__name__)
            return FetchResult(
                url=url,
                final_url=url,
                status=None,
                html="",
                engine="browser",
                error=f"browser_unavailable:{type(exc).__name__}",
                elapsed_ms=elapsed(),
            )

        await self._throttle((url.split("/")[2] if "://" in url else url).split(":")[0])
        page = None
        async with self._semaphore:
            try:
                page = await self._context.new_page()

                async def route_handler(route: Any) -> None:
                    if route.request.resource_type in _BLOCKED_RESOURCES:
                        await route.abort()
                    elif _unsafe_browser_request(route.request.url):
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", route_handler)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if settle > 0:
                    try:
                        await page.wait_for_load_state("load", timeout=settle)
                    except Exception:
                        # Ad-heavy shops never fire `load`; the DOM is enough.
                        pass
                html = await page.content()
                status = response.status if response is not None else 0
                headers = dict(response.headers) if response is not None else {}
                unsupported = content_type_error(
                    {key.lower(): value for key, value in headers.items()}
                )
                if unsupported is not None:
                    return FetchResult(
                        url=url,
                        final_url=page.url,
                        status=status or None,
                        html="",
                        headers={key.lower(): value for key, value in headers.items()},
                        engine="browser",
                        error=unsupported,
                        elapsed_ms=elapsed(),
                    )
                encoded = html.encode("utf-8", errors="replace")
                if len(encoded) > cap:
                    html = encoded[:cap].decode("utf-8", errors="ignore")
                    encoded = html.encode("utf-8", errors="replace")
                final_url = page.url
                error = None if 0 < status < 400 else f"http_{status}"
                log.debug(
                    "browser.fetch",
                    url=final_url,
                    status=status,
                    bytes=len(encoded),
                )
                return FetchResult(
                    url=url,
                    final_url=final_url,
                    status=status or None,
                    html=html,
                    headers={key.lower(): value for key, value in headers.items()},
                    engine="browser",
                    error=error,
                    bytes=len(encoded),
                    elapsed_ms=elapsed(),
                )
            except Exception as exc:
                name = type(exc).__name__
                log.debug("browser.fetch.error", url=url, error=name)
                return FetchResult(
                    url=url,
                    final_url=url,
                    status=None,
                    html="",
                    engine="browser",
                    error="timeout" if "Timeout" in name else f"browser:{name}",
                    elapsed_ms=elapsed(),
                )
            finally:
                if page is not None:
                    await page.close()
