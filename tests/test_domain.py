from __future__ import annotations

import pytest

from forgesmith.domain import LeadCandidate, LeadSource, UpsertStats, normalize_url, parse_domain


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://WWW.Example.pl/path/?utm_source=x&a=1", "https://example.pl/path?a=1"),
        ("example.pl", "https://example.pl/"),
        ("http://example.pl:8080/x/", "http://example.pl:8080/x"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.example.pl/kontakt", "example.pl"),
        ("example.pl", "example.pl"),
        ("", None),
        ("not-a-domain", None),
    ],
)
def test_parse_domain(raw: str, expected: str | None) -> None:
    assert parse_domain(raw) == expected


def test_from_site_derives_domain() -> None:
    candidate = LeadCandidate.from_site(
        source=LeadSource.SHOPIFY,
        source_id="example.pl",
        name="Example",
        site_url="https://www.example.pl/",
    )
    assert candidate.domain == "example.pl"


def test_marketplace_lead_keeps_explicit_domain() -> None:
    candidate = LeadCandidate(
        source=LeadSource.OLX,
        source_id="olx:42",
        name="Mebis",
        url="https://www.olx.pl/d/oferta/xyz-ID1.html",
    )
    assert candidate.domain is None


def test_upsert_stats_adds() -> None:
    first = UpsertStats(inserted=1, updated=2, duplicates=3)
    second = UpsertStats(inserted=1, updated=1, duplicates=0)
    assert first + second == UpsertStats(inserted=2, updated=3, duplicates=3)
