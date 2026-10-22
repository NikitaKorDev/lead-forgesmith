from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from httpx import Response

from forgesmith.http import HostPacer, PoliteClient


@pytest.fixture
async def client() -> AsyncIterator[PoliteClient]:
    async with PoliteClient() as polite:
        yield polite


async def test_host_pacer_shares_delay_per_host() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    pacer = HostPacer(1.5, clock=clock, sleep=sleep)
    await pacer.wait("a.pl")
    await pacer.wait("a.pl")
    await pacer.wait("b.pl")
    assert sleeps == [1.5]


@respx.mock
async def test_fetch_returns_status_headers_and_final_url(client: PoliteClient) -> None:
    respx.get("https://a.pl/kontakt").mock(
        return_value=Response(200, text="<html>Kontakt</html>", headers={"X-Test": "1"})
    )

    result = await client.fetch("https://a.pl/kontakt")

    assert result.status == 200
    assert result.final_url == "https://a.pl/kontakt"
    assert result.error is None
    assert result.headers["x-test"] == "1"
    assert result.html == "<html>Kontakt</html>"
    assert result.bytes == len(b"<html>Kontakt</html>")
    assert result.engine == "http"


@respx.mock
async def test_fetch_follows_redirects_manually(client: PoliteClient) -> None:
    respx.get("http://a.pl/").mock(return_value=Response(301, headers={"Location": "/home"}))
    respx.get("http://a.pl/home").mock(
        return_value=Response(302, headers={"Location": "https://a.pl/final"})
    )
    respx.get("https://a.pl/final").mock(return_value=Response(200, text="ok"))

    result = await client.fetch("http://a.pl/")

    assert result.status == 200
    assert result.final_url == "https://a.pl/final"
    assert result.html == "ok"


@respx.mock
async def test_fetch_distinguishes_403_and_timeout(client: PoliteClient) -> None:
    respx.get("https://blocked.pl/").mock(return_value=Response(403, text="nope"))
    respx.get("https://slow.pl/").mock(side_effect=httpx.ReadTimeout("slow"))

    blocked = await client.fetch("https://blocked.pl/")
    slow = await client.fetch("https://slow.pl/")

    assert blocked.status == 403
    assert blocked.error == "http_403"
    assert slow.status is None
    assert slow.error == "timeout"


@respx.mock
async def test_fetch_caps_response_bytes(client: PoliteClient) -> None:
    respx.get("https://big.pl/").mock(return_value=Response(200, text="x" * 100))

    result = await client.fetch("https://big.pl/", max_bytes=10)

    assert result.status == 200
    assert result.html == "x" * 10
    assert result.bytes == 10


@respx.mock
async def test_fetch_rejects_literal_private_ip_without_request(client: PoliteClient) -> None:
    route = respx.get("http://127.0.0.1/").mock(return_value=Response(200, text="secret"))

    result = await client.fetch("http://127.0.0.1/")

    assert result.status is None
    assert result.error == "unsafe_url:private_ip"
    assert not route.called


@respx.mock
async def test_fetch_validates_redirect_hops(client: PoliteClient) -> None:
    respx.get("https://a.pl/").mock(
        return_value=Response(302, headers={"Location": "http://169.254.169.254/latest"})
    )

    result = await client.fetch("https://a.pl/")

    assert result.status == 302
    assert result.final_url == "http://169.254.169.254/latest"
    assert result.error is not None
    assert result.error.startswith("blocked_redirect:private_ip")


@respx.mock
async def test_fetch_stops_after_three_redirects(client: PoliteClient) -> None:
    for index in range(4):
        respx.get(f"https://a.pl/{index}").mock(
            return_value=Response(302, headers={"Location": f"/{index + 1}"})
        )

    result = await client.fetch("https://a.pl/0")

    assert result.error == "too_many_redirects"
    assert result.status == 302


async def test_fetch_rejects_non_http_scheme(client: PoliteClient) -> None:
    result = await client.fetch("file:///etc/passwd")
    assert result.error == "unsafe_url:unsupported_scheme"


@respx.mock
async def test_fetch_rejects_binary_content_type(client: PoliteClient) -> None:
    respx.get("https://a.pl/file.pdf").mock(
        return_value=Response(200, content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"})
    )

    result = await client.fetch("https://a.pl/file.pdf")

    assert result.status == 200
    assert result.error == "content_type:application/pdf"
    assert result.html == ""


class FakeHTTP:
    def __init__(self, mapping: dict[str, object]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def fetch(self, url: str, **kwargs: object):
        self.calls.append(url)
        value = self.mapping.get(url)
        if callable(value):
            return value()
        return value


class FakeBrowser(FakeHTTP):
    pass


def snapshot(url: str, html: str = "<html>ok</html>", status: int = 200, engine: str = "http"):
    from forgesmith.enrichment.models import FetchResult

    return FetchResult(
        url=url,
        final_url=url,
        status=status,
        html=html,
        engine=engine,
        bytes=len(html),
    )


async def test_site_fetcher_escalates_and_sticks_to_browser(monkeypatch) -> None:
    from forgesmith.config import settings
    from forgesmith.enrichment.fetch import SiteFetcher

    monkeypatch.setattr(settings, "enrich_respect_robots", False)
    http = FakeHTTP({"https://a.pl/": snapshot("https://a.pl/", status=403)})
    browser = FakeBrowser(
        {
            "https://a.pl/": snapshot("https://a.pl/", engine="browser"),
            "https://a.pl/kontakt": snapshot("https://a.pl/kontakt", engine="browser"),
        }
    )
    fetcher = SiteFetcher(settings=settings, http=http, browser=browser)

    first = await fetcher.fetch("https://a.pl/")
    second = await fetcher.fetch("https://a.pl/kontakt")

    assert first.engine == "browser"
    assert second.engine == "browser"
    assert http.calls == ["https://a.pl/"]
    assert browser.calls == ["https://a.pl/", "https://a.pl/kontakt"]


async def test_site_fetcher_escalates_on_challenge_page(monkeypatch) -> None:
    from forgesmith.config import settings
    from forgesmith.enrichment.fetch import SiteFetcher

    monkeypatch.setattr(settings, "enrich_respect_robots", False)
    http = FakeHTTP(
        {"https://a.pl/": snapshot("https://a.pl/", "<html><title>Just a moment...</title></html>")}
    )
    browser = FakeBrowser({"https://a.pl/": snapshot("https://a.pl/", engine="browser")})
    result = await SiteFetcher(settings=settings, http=http, browser=browser).fetch("https://a.pl/")
    assert result.engine == "browser"
    assert result.error is None


async def test_site_fetcher_keeps_http_result_when_browser_is_unavailable(monkeypatch) -> None:
    from forgesmith.config import settings
    from forgesmith.enrichment.fetch import SiteFetcher

    monkeypatch.setattr(settings, "enrich_respect_robots", False)
    http = FakeHTTP({"https://a.pl/": snapshot("https://a.pl/", status=403)})
    browser = FakeBrowser(
        {"https://a.pl/": snapshot("https://a.pl/", status=None, engine="browser")}
    )
    browser.mapping["https://a.pl/"].error = "browser_unavailable:ImportError"

    result = await SiteFetcher(settings=settings, http=http, browser=browser).fetch("https://a.pl/")
    assert result.engine == "http"
    assert result.status == 403


async def test_site_fetcher_respects_robots(monkeypatch) -> None:
    from forgesmith.config import settings
    from forgesmith.enrichment.fetch import SiteFetcher

    monkeypatch.setattr(settings, "enrich_respect_robots", True)
    http = FakeHTTP(
        {
            "https://a.pl/robots.txt": snapshot(
                "https://a.pl/robots.txt", "User-agent: *\nDisallow: /private"
            )
        }
    )
    result = await SiteFetcher(settings=settings, http=http, browser=None).fetch(
        "https://a.pl/private"
    )
    assert result.error == "robots_disallowed"
    assert http.calls == ["https://a.pl/robots.txt"]


async def test_site_fetcher_deadline_returns_without_fetching(monkeypatch) -> None:
    import time

    from forgesmith.config import settings
    from forgesmith.enrichment.fetch import SiteFetcher

    monkeypatch.setattr(settings, "enrich_respect_robots", False)
    http = FakeHTTP({})
    result = await SiteFetcher(settings=settings, http=http, browser=None).fetch(
        "https://a.pl/", deadline=time.monotonic() - 1
    )
    assert result.error == "deadline"
    assert http.calls == []
