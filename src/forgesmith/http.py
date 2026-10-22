"""Polite async HTTP: per-host rate limiting, retries, a real UA."""

from __future__ import annotations

import asyncio
import ipaddress
import random
import time
from collections import defaultdict
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx
import structlog

from forgesmith.config import Settings
from forgesmith.config import settings as default_settings
from forgesmith.enrichment.models import FetchResult

log = structlog.get_logger(__name__)

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_PRIVATE_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
}
_TEXT_CONTENT_TYPES = (
    "text/",
    "application/xhtml+xml",
    "application/xml",
    "application/json",
)


class HostPacer:
    """One minimum-interval clock per host, shared by HTTP and Chromium.

    SiteFetcher passes the same instance to both clients, so a host that needed
    a browser escalation is not hit twice in quick succession by two engines.
    """

    def __init__(
        self,
        delay: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.delay = delay
        self._clock = clock
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_hit: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        if self.delay <= 0:
            return
        async with self._locks[host]:
            if host not in self._last_hit:
                self._last_hit[host] = self._clock()
                return
            elapsed = self._clock() - self._last_hit[host]
            remaining = self.delay - elapsed
            if remaining > 0:
                result = self._sleep(remaining)
                if asyncio.iscoroutine(result):
                    await result
            self._last_hit[host] = self._clock()


def validate_public_url(url: str) -> str | None:
    """Reject non-HTTP(S) and SSRF-prone literal hosts. Returns ``None`` if safe."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "invalid_url"
    if parts.scheme.lower() not in ("http", "https"):
        return "unsupported_scheme"
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return "missing_host"
    if parts.username or parts.password:
        return "credentials_not_allowed"
    if host in _PRIVATE_HOSTS or host.endswith((".localhost", ".local", ".internal")):
        return "private_host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_global:
        return "private_ip"
    return None


def content_type_error(headers: dict[str, str]) -> str | None:
    """Reject binary payloads before decoding; an absent header is allowed."""
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type and not content_type.startswith(_TEXT_CONTENT_TYPES):
        return f"content_type:{content_type}"
    return None


class PoliteClient:
    """Wraps httpx with a per-host minimum interval and a global concurrency cap."""

    def __init__(self, settings: Settings | None = None, *, pacer: HostPacer | None = None) -> None:
        self.settings = settings or default_settings
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self.pacer = pacer or HostPacer(self.settings.request_delay)

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
        await self.pacer.wait(host)

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

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        request_timeout: float | None = None,
    ) -> FetchResult:
        """GET with manual redirects, an SSRF check per hop, and a hard byte cap.

        Unlike :meth:`get`, this keeps the status/headers/error so enrichment can
        distinguish a 403 (blocked) from a timeout (transient).
        """
        started = time.monotonic()
        cap = max_bytes or self.settings.enrich_max_bytes
        per_request_timeout = request_timeout or self.settings.request_timeout
        current = url
        redirects = 0
        last_status: int | None = None

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        while True:
            unsafe = validate_public_url(current)
            if unsafe is not None:
                prefix = "blocked_redirect" if redirects else "unsafe_url"
                return FetchResult(
                    url=url,
                    final_url=current,
                    status=last_status,
                    html="",
                    engine="http",
                    error=f"{prefix}:{unsafe}",
                    elapsed_ms=elapsed(),
                )

            host = (urlsplit(current).hostname or "").lower()
            try:
                async with self._semaphore:
                    await self._throttle(host)
                    request = self.client.build_request("GET", current, timeout=per_request_timeout)
                    response = await self.client.send(request, stream=True, follow_redirects=False)
                try:
                    status = response.status_code
                    last_status = status
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    location = response.headers.get("location")
                    if status in _REDIRECT_STATUS and location:
                        if redirects >= 3:
                            return FetchResult(
                                url=url,
                                final_url=current,
                                status=status,
                                html="",
                                headers=headers,
                                engine="http",
                                error="too_many_redirects",
                                elapsed_ms=elapsed(),
                            )
                        current = urljoin(current, location)
                        redirects += 1
                        continue

                    unsupported = content_type_error(headers)
                    if unsupported is not None:
                        return FetchResult(
                            url=url,
                            final_url=current,
                            status=status,
                            html="",
                            headers=headers,
                            engine="http",
                            error=unsupported,
                            elapsed_ms=elapsed(),
                        )

                    body = await self._read_limited(response, cap)
                    html = body.decode(response.encoding or "utf-8", errors="replace")
                    error = None if status < 400 else f"http_{status}"
                    log.debug(
                        "http.fetch", url=current, status=status, bytes=len(body), hops=redirects
                    )
                    return FetchResult(
                        url=url,
                        final_url=current,
                        status=status,
                        html=html,
                        headers=headers,
                        engine="http",
                        error=error,
                        bytes=len(body),
                        elapsed_ms=elapsed(),
                    )
                finally:
                    await response.aclose()
            except httpx.TimeoutException:
                log.debug("http.fetch.timeout", url=current, hops=redirects)
                return FetchResult(
                    url=url,
                    final_url=current,
                    status=None,
                    html="",
                    engine="http",
                    error="timeout",
                    elapsed_ms=elapsed(),
                )
            except httpx.HTTPError as exc:
                log.debug("http.fetch.error", url=current, error=type(exc).__name__)
                return FetchResult(
                    url=url,
                    final_url=current,
                    status=None,
                    html="",
                    engine="http",
                    error=f"network:{type(exc).__name__}",
                    elapsed_ms=elapsed(),
                )

    @staticmethod
    async def _read_limited(response: httpx.Response, cap: int) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            remaining = cap - len(body)
            if remaining <= 0:
                break
            body.extend(chunk[:remaining])
            if len(chunk) > remaining:
                break
        return bytes(body)
