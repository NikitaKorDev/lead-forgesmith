from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import respx
from httpx import Response

from forgesmith.http import PoliteClient
from forgesmith.sources.shopify import ShopifySource, load_seeds, seeds_from_paths

PRODUCTS = {
    "products": [
        {
            "title": "Krzesło dębowe Loft",
            "vendor": "Inny Producent",
            "product_type": "Krzesła",
            "variants": [{"price": "399.00"}],
        },
        {
            "title": "Stół dębowy Loft",
            "vendor": "Inny Producent",
            "product_type": "Stoły",
            "variants": [{"price": "1299.00"}],
        },
    ]
}

HOMEPAGE = """<html><head>
<meta property="og:site_name" content="Drewno i Spółka">
</head><body><a href="https://cdn.shopify.com/x">x</a></body></html>"""


@pytest.fixture
async def client() -> AsyncIterator[PoliteClient]:
    async with PoliteClient() as polite:
        yield polite


def _source(client: PoliteClient, seeds: list[str]) -> ShopifySource:
    return ShopifySource(client, seeds=seeds)


@respx.mock
async def test_inspect_shopify_store(client: PoliteClient) -> None:
    respx.get("https://dobresklep.pl/products.json?limit=250").mock(
        return_value=Response(200, json=PRODUCTS)
    )
    respx.get("https://dobresklep.pl/").mock(return_value=Response(200, text=HOMEPAGE))

    candidate = await _source(client, []).inspect("https://www.dobresklep.pl/kontakt")

    assert candidate is not None
    assert candidate.source_id == "dobresklep.pl"
    assert candidate.domain == "dobresklep.pl"
    assert candidate.name == "Drewno i Spółka"
    assert candidate.category == "Krzesła"
    assert candidate.platform == "shopify"
    assert candidate.raw["product_count"] == 2
    assert candidate.raw["detected_via"] == "products.json"


@respx.mock
async def test_inspect_falls_back_to_html_marker(client: PoliteClient) -> None:
    respx.get("https://cdnonly.pl/products.json?limit=250").mock(return_value=Response(404))
    respx.get("https://cdnonly.pl/collections.json?limit=50").mock(return_value=Response(404))
    respx.get("https://cdnonly.pl/").mock(
        return_value=Response(
            200,
            text=(
                "<html><head><title>Sklep | CDN Only</title></head>"
                '<body><link href="https://cdn.shopify.com/s/files/x.css"></body></html>'
            ),
        )
    )

    candidate = await _source(client, []).inspect("cdnonly.pl")

    assert candidate is not None
    assert candidate.name == "CDN Only"
    assert candidate.raw["detected_via"] == "html_marker"


@respx.mock
async def test_inspect_returns_none_for_non_shopify(client: PoliteClient) -> None:
    respx.get("https://plain.pl/products.json?limit=250").mock(return_value=Response(404))
    respx.get("https://plain.pl/collections.json?limit=50").mock(return_value=Response(404))
    respx.get("https://plain.pl/").mock(return_value=Response(200, text="<html>WooCommerce</html>"))

    assert await _source(client, []).inspect("plain.pl") is None


@respx.mock
async def test_discover_respects_limit(client: PoliteClient) -> None:
    respx.get("https://a.pl/products.json?limit=250").mock(
        return_value=Response(200, json=PRODUCTS)
    )
    respx.get("https://a.pl/").mock(return_value=Response(200, text=HOMEPAGE))
    respx.get("https://b.pl/products.json?limit=250").mock(
        return_value=Response(200, json=PRODUCTS)
    )
    respx.get("https://b.pl/").mock(return_value=Response(200, text=HOMEPAGE))

    source = ShopifySource(client, seeds=["a.pl", "b.pl"], limit=1)
    collected = [c async for c in source.discover()]

    assert len(collected) == 1


def test_load_seeds_skips_comments_and_blanks(tmp_path) -> None:
    path = tmp_path / "seeds.txt"
    path.write_text("# comment\na.pl\n\n  b.pl  # trailing\n", encoding="utf-8")
    assert load_seeds(path) == ["a.pl", "b.pl"]


def test_seeds_from_paths_dedupes(tmp_path) -> None:
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("a.pl\nb.pl\n", encoding="utf-8")
    second.write_text("https://www.a.pl/\nc.pl\n", encoding="utf-8")
    assert seeds_from_paths([first, second]) == ["a.pl", "b.pl", "c.pl"]


def test_load_seeds_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_seeds(tmp_path / "nope.txt")
