"""Polite async HTTP: per-host rate limiting, retries, a real UA."""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict

import httpx
import structlog

from forgesmith.config import Settings
from forgesmith.config import settings as default_settings

log = structlog.get_logger(__name__)

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class PoliteClient:
    """Wraps httpx with a per-host minimum interval and a global concurrency cap."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_hit: dict[str, float] = {}

    async def __aenter__(self) -> PoliteClient:
        self._client = httpx.AsyncClient(
            timeout=self.settings.request_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
            },
            limits=httpx.Limits(max_connections=self.settings.max_concurrency * 2),
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("PoliteClient used outside of 'async with'")
        return self._client

    async def _throttle(self, host: str) -> None:
        async with self._host_locks[host]:
            elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
            wait = self.settings.request_delay - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[host] = time.monotonic()

    async def get(self, url: str, **kwargs: object) -> httpx.Response | None:
        """GET with retries. Returns None when the URL is unreachable/blocked."""
        host = httpx.URL(url).host or url
        async with self._semaphore:
            for attempt in range(self.settings.max_retries + 1):
                await self._throttle(host)
                try:
                    response = await self.client.get(url, **kwargs)
                except httpx.HTTPError as exc:
                    log.debug("http.error", url=url, attempt=attempt, error=str(exc))
                    if attempt >= self.settings.max_retries:
                        return None
                    await asyncio.sleep(2**attempt + random.random())
                    continue

                if response.status_code in _RETRY_STATUS and attempt < self.settings.max_retries:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after or "").isdigit() else 2**attempt
                    log.debug("http.retry", url=url, status=response.status_code, delay=delay)
                    await asyncio.sleep(delay + random.random())
                    continue

                if response.status_code >= 400:
                    log.debug("http.status", url=url, status=response.status_code)
                    return None
                return response
        return None

    async def get_json(self, url: str, **kwargs: object) -> object | None:
        response = await self.get(url, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            log.debug("http.bad_json", url=url)
            return None
