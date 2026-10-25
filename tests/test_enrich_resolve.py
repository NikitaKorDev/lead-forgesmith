from __future__ import annotations

from types import SimpleNamespace

from forgesmith.enrichment.models import PageSnapshot
from forgesmith.enrichment.resolve import (
    extract_external_domain,
    load_overrides,
    resolve_domain,
)


class FakeFetcher:
    def __init__(self, pages: dict[str, PageSnapshot]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(self, url: str, **kwargs: object) -> PageSnapshot:
        self.calls.append(url)
        return self.pages.get(
            url,
            PageSnapshot(
                url=url,
                final_url=url,
                status=404,
                html="",
                engine="http",
                bytes=0,
                elapsed_ms=0,
                error="http_404",
            ),
        )


def lead(**kwargs: object) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "domain": None,
        "url": "https://www.olx.pl/d/oferta/x-ID1.html",
        "raw": {"ad": {}},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_load_overrides_reads_valid_rows_and_skips_blank(tmp_path) -> None:
    path = tmp_path / "overrides.csv"
    path.write_text(
        "lead_id,domain,note\n1,sklep.example.pl,tak\n2,\n3,facebook.com,no\nx,bad.pl,no\n",
        encoding="utf-8",
    )
    assert load_overrides(path) == {1: "sklep.example.pl"}


def test_load_overrides_missing_file_is_empty(tmp_path) -> None:
    assert load_overrides(tmp_path / "missing.csv") == {}


def test_extract_external_domain_prefers_website_hint_and_skips_social() -> None:
    html = """
    <a href="https://facebook.com/sklep">Facebook</a>
    <a href="https://www.olx.pl/pomoc">OLX</a>
    <a href="https://sklep.example.pl">Odwiedź sklep</a>
    <a href="https://other.pl">Inna firma</a>
    """
    assert (
        extract_external_domain(html, "https://regama.olx.pl/", own_domain="olx.pl")
        == "sklep.example.pl"
    )


async def test_source_domain_shortcuts_resolution() -> None:
    fetcher = FakeFetcher({})
    resolved = await resolve_domain(lead(domain="a.pl"), fetcher)
    assert resolved is not None
    assert (resolved.domain, resolved.source) == ("a.pl", "source")
    assert fetcher.calls == []


async def test_override_is_used_before_network() -> None:
    fetcher = FakeFetcher({})
    resolved = await resolve_domain(lead(id=7), fetcher, {7: "override.pl"})
    assert resolved is not None
    assert (resolved.domain, resolved.source) == ("override.pl", "override")
    assert fetcher.calls == []


async def test_raw_external_url_is_not_a_resolution_branch() -> None:
    fetcher = FakeFetcher({})
    resolved = await resolve_domain(
        lead(raw={"ad": {"external_url": "https://sklep.przyklad.pl"}}), fetcher
    )
    assert resolved is None
    assert fetcher.calls == []


async def test_shop_subdomain_page_resolves_external_storefront() -> None:
    shop_url = "https://regama.olx.pl/home/?tab=contact"
    shop = PageSnapshot(
        url=shop_url,
        final_url=shop_url,
        status=200,
        html='<a href="https://regama.pl">Strona www</a>',
        engine="browser",
        bytes=10,
        elapsed_ms=1,
    )
    fetcher = FakeFetcher({shop_url: shop})
    resolved = await resolve_domain(lead(id=38, raw={"ad": {"shop_subdomain": "regama"}}), fetcher)
    assert resolved is not None
    assert (resolved.domain, resolved.source) == ("regama.pl", "shop_page")
    assert resolved.evidence["checked"] == shop_url


def test_extract_external_domain_ignores_olx_corporate_footer() -> None:
    html = """
    <a href="https://www.olxgroup.com/">Grupa OLX</a>
    <a href="https://www.otomoto.pl/">Otomoto</a>
    <a href="https://www.obido.pl/">obido</a>
    <a href="https://itunes.apple.com/pl/app/olx.pl">Aplikacja</a>
    """
    assert (
        extract_external_domain(
            html,
            "https://proone.olx.pl/home/?tab=contact",
            own_domain="olx.pl",
        )
        is None
    )


async def test_offer_page_only_used_with_deep_flag() -> None:
    offer_url = "https://www.olx.pl/d/oferta/x-ID1.html"
    offer = PageSnapshot(
        url=offer_url,
        final_url=offer_url,
        status=200,
        html='<a href="https://gleboki.pl">Zobacz sklep</a>',
        engine="browser",
        bytes=10,
        elapsed_ms=1,
    )
    fetcher = FakeFetcher({offer_url: offer})

    assert await resolve_domain(lead(), fetcher) is None
    resolved = await resolve_domain(lead(), fetcher, deep=True)
    assert resolved is not None
    assert (resolved.domain, resolved.source) == ("gleboki.pl", "offer_page")


async def test_unresolved_lead_returns_none() -> None:
    assert await resolve_domain(lead(), FakeFetcher({})) is None
