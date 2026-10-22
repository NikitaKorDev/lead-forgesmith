"""HTTP-first site fetcher with per-host sticky browser escalation."""

from __future__ import annotations

import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import structlog

from forgesmith.browser import BrowserFetcher
from forgesmith.config import Settings
from forgesmith.config import settings as default_settings
from forgesmith.enrichment.models import FetchResult, PageSnapshot
from forgesmith.enrichment.site import is_challenge_page, is_js_shell
from forgesmith.http import HostPacer, PoliteClient

log = structlog.get_logger(__name__)

_ENGINES = {"auto", "http", "browser"}
_BLOCK_STATUS = {401, 403, 429, 503}


class SiteFetcher:
    """One pacer, two engines, one sticky engine decision per host.

    Sequential use is intentional: ``request_delay`` already serializes requests
    to a host, so concurrency buys no throughput for this workload.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http: PoliteClient | None = None,
        browser: BrowserFetcher | None = None,
        engine: str = "auto",
    ) -> None:
        if engine not in _ENGINES:
            raise ValueError(f"engine must be one of {sorted(_ENGINES)}, got {engine!r}")
        self.settings = settings or default_settings
        self.engine = engine
        self.pacer = HostPacer(self.settings.request_delay)
        self._http = http
        self._browser = browser
        self._owns_http = False
        self._owns_browser = False
        self._engine_by_host: dict[str, str] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    async def __aenter__(self) -> SiteFetcher:
        if self._http is None:
            self._http = PoliteClient(self.settings, pacer=self.pacer)
            await self._http.__aenter__()
            self._owns_http = True
        if self._browser is None and self.engine != "http":
            self._browser = BrowserFetcher(self.settings, pacer=self.pacer)
            self._owns_browser = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_http and self._http is not None:
            await self._http.__aexit__(*exc_info)
        if self._owns_browser and self._browser is not None:
            await self._browser.__aexit__(*exc_info)

    # ----------------------------------------------------------------- public
    async def fetch(
        self,
        url: str,
        *,
        deadline: float | None = None,
        force_engine: str | None = None,
    ) -> PageSnapshot:
        started = time.monotonic()
        if deadline is not None and started >= deadline:
            return self._error(url, "deadline", "http", started)

        if not await self._allowed_by_robots(url):
            return self._error(url, "robots_disallowed", "http", started)

        host = (urlsplit(url).hostname or "").lower()
        engine = force_engine or self._engine_by_host.get(host)
        if engine is None:
            engine = "browser" if self.engine == "browser" else "http"

        timeout = self._page_timeout(deadline)
        if timeout <= 0:
            return self._error(url, "deadline", engine, started)

        if engine == "browser":
            result = await self._fetch_browser(url, timeout, started)
        else:
            result = await self._fetch_http(url, timeout, started)
            if (
                self.engine == "auto"
                and self._browser is not None
                and self._should_escalate(result)
            ):
                self._engine_by_host[host] = "browser"
                alt = await self._fetch_browser(url, timeout, started)
                if alt.error and alt.error.startswith("browser_unavailable"):
                    return self._snapshot(result)
                result = alt
            else:
                self._engine_by_host.setdefault(host, "http")

        return self._snapshot(result)

    # ---------------------------------------------------------------- engines
    async def _fetch_http(self, url: str, budget: float, started: float) -> FetchResult:
        assert self._http is not None  # entered via __aenter__
        return await self._http.fetch(url, request_timeout=budget)

    async def _fetch_browser(self, url: str, budget: float, started: float) -> FetchResult:
        if self._browser is None:
            return FetchResult(
                url=url,
                final_url=url,
                status=None,
                html="",
                engine="browser",
                error="browser_unavailable:not_configured",
            )
        return await self._browser.fetch(url, request_timeout=budget)

    @staticmethod
    def _should_escalate(result: FetchResult) -> bool:
        if result.status in _BLOCK_STATUS:
            return True
        if result.error and (result.error == "timeout" or result.error.startswith("network:")):
            return True
        return is_challenge_page(result) or is_js_shell(result)

    # ----------------------------------------------------------------- robots
    async def _allowed_by_robots(self, url: str) -> bool:
        if not self.settings.enrich_respect_robots:
            return True
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host:
            return False
        if host not in self._robots:
            self._robots[host] = await self._load_robots(parts.scheme or "https", host)
        parser = self._robots[host]
        if parser is None:
            return True
        return parser.can_fetch(self.settings.user_agent, url)

    async def _load_robots(self, scheme: str, host: str) -> RobotFileParser | None:
        if self._http is None:
            return None
        robots_url = f"{scheme}://{host}/robots.txt"
        try:
            result = await self._http.fetch(robots_url, max_bytes=500_000, request_timeout=5.0)
        except Exception:  # pragma: no cover - defensive parity with sources
            return None
        if result.status is None or result.status >= 400 or not result.html:
            return None
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(result.html.splitlines())
        return parser

    # ---------------------------------------------------------------- helpers
    def _page_timeout(self, deadline: float | None) -> float:
        timeout = self.settings.enrich_page_timeout
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        return max(0.0, timeout)

    @staticmethod
    def _snapshot(result: FetchResult) -> PageSnapshot:
        return PageSnapshot(
            url=result.url,
            final_url=result.final_url,
            status=result.status,
            html=result.html,
            engine=result.engine,
            bytes=result.bytes,
            elapsed_ms=result.elapsed_ms,
            headers=result.headers,
            error=result.error,
        )

    @staticmethod
    def _error(url: str, error: str, engine: str, started: float) -> PageSnapshot:
        return PageSnapshot(
            url=url,
            final_url=url,
            status=None,
            html="",
            engine=engine,
            bytes=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )
