from __future__ import annotations

import json
from typing import Any, cast

from forgesmith.sources.olx import OlxSource

ADS = [
    {
        "id": 111,
        "title": "Pufa 5w1 smart meble",
        "url": "https://www.olx.pl/d/oferta/pufa-5w1-CID628-ID1bhXFH.html",
        "isBusiness": True,
        "contact": {"name": "Mebis", "phone": True},
        "user": {"id": 900, "name": "Mebis"},
        "shop": {"subdomain": None},
        "location": {"cityName": "Warszawa", "regionName": "Mazowieckie"},
        "price": {"displayValue": "590 zł"},
        "externalUrl": "https://mebis.pl/",
    },
    {
        "id": 222,
        "title": "Krzesło tapicerowane",
        "url": "https://www.olx.pl/d/oferta/krzeslo-CID628-ID2abc.html",
        "isBusiness": True,
        "contact": {"name": "Mebis"},
        "user": {"id": 900, "name": "Mebis"},
        "shop": {"subdomain": None},
        "location": {"cityName": "Warszawa"},
    },
    {
        "id": 333,
        "title": "Meble do oddania za darmo",
        "url": "https://www.olx.pl/d/oferta/meble-darmo-CID628-ID3xyz.html",
        "isBusiness": False,
        "contact": {"name": "Marta"},
        "user": {"id": 901, "name": "Marta"},
        "shop": {"subdomain": None},
        "location": {"cityName": "Warszawa"},
    },
]


def build_html(ads: list[dict[str, Any]]) -> str:
    state = {"listing": {"listing": {"ads": ads}}}
    literal = json.dumps(json.dumps(state, ensure_ascii=False))
    return f"<html><body><script>window.__PRERENDERED_STATE__= {literal};</script></body></html>"


def make_source(**kwargs: Any) -> OlxSource:
    return OlxSource(cast(Any, None), **kwargs)


def test_parse_keeps_business_and_dedupes_by_seller() -> None:
    source = make_source(queries=["meble"], cities=["warszawa"])
    candidates = source.parse_listing(build_html(ADS), category="meble")

    assert len(candidates) == 1, "two ads from the same seller collapse into one lead"
    lead = candidates[0]
    assert lead.source == "olx"
    assert lead.source_id == "olx:900"
    assert lead.name == "Mebis"
    assert lead.city == "Warszawa"
    assert lead.country == "PL"
    assert lead.category == "meble"
    assert lead.domain == "mebis.pl"
    assert lead.raw["ad"]["title"] == "Pufa 5w1 smart meble"
    assert lead.raw["ad"]["price"] == "590 zł"


def test_private_sellers_are_skipped() -> None:
    source = make_source(queries=["meble"])
    candidates = source.parse_listing(build_html([ADS[2]]))
    assert candidates == []


def test_anchor_fallback() -> None:
    html = '<a href="/d/oferta/stol-debowy-CID628-ID4qq.html" title="Stół dębowy">Stół</a>'
    candidates = make_source(queries=["stol"]).parse_listing(html, category="stol")
    # No business signal in bare anchors, so nothing is collected.
    assert candidates == []


def test_listing_urls_builds_query_and_category_paths() -> None:
    source = make_source(
        queries=["meble dębowe"],
        categories=["nieruchomosci/mieszkania"],
        cities=["warszawa"],
        max_pages=2,
    )
    urls = source.listing_urls()
    assert "https://www.olx.pl/oferty/warszawa/q-meble-debowe/" in urls
    assert "https://www.olx.pl/oferty/warszawa/q-meble-debowe/?page=2" in urls
    assert "https://www.olx.pl/nieruchomosci/mieszkania/warszawa/" in urls


def test_listing_urls_without_city() -> None:
    urls = make_source(queries=["meble"], max_pages=1).listing_urls()
    assert urls == ["https://www.olx.pl/oferty/q-meble/"]


def test_garbage_json_does_not_raise() -> None:
    source = make_source(queries=["meble"])
    assert source.parse_listing("<html>window.__PRERENDERED_STATE__= oops;</html>") == []
