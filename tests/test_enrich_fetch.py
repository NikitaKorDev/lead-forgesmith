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
