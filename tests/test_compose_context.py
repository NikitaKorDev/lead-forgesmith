from __future__ import annotations

import pytest

from forgesmith.compose.context import (
    build_fact_payload,
    canonical_json,
    context_hash,
    payload_is_insufficient,
)
from forgesmith.compose.models import FactPayload
from forgesmith.db.models import Enrichment, EnrichmentStatusDB, Lead


def _lead(
    *,
    source: str = "shopify",
    name: str = "Sklep",
    legal_name: str | None = "Sklep sp. z o.o.",
    description: str = "Tworzymy meble z litego drewna.",
    context: dict | None = None,
) -> Lead:
    lead = Lead(
        id=1,
        source=source,
        source_id="a.pl",
        name=name,
        url="https://a.pl/",
        domain="a.pl",
        status="enriched",
    )
    lead.enrichment = Enrichment(
        lead_id=1,
        status=EnrichmentStatusDB.OK,
        domain="a.pl",
        storefront_platform="shopify",
        legal_name=legal_name,
        description=description,
        context=context
        or {
            "text_excerpt": "Używamy plików cookie.\nTworzymy meble z litego drewna.",
            "headings": ["Nasza pracownia"],
            "products": [{"title": "Stół dębowy"}, "0"],
            "pages": ["https://a.pl/secret"],
        },
        evidence={"emails": ["hidden@a.pl"], "nip": "1234567890"},
    )
    return lead


def test_payload_contains_only_cleaned_safe_fields() -> None:
    lead = _lead()
    payload = build_fact_payload(lead, "role")

    assert payload.domain == "a.pl"
    assert payload.company_name == "Sklep sp. z o.o."
    assert payload.description == "Tworzymy meble z litego drewna."
    assert "cookie" not in payload.text_excerpt.casefold()
    assert "Tworzymy meble z litego drewna." in payload.text_excerpt
    assert payload.headings == ["Nasza pracownia"]
    assert payload.assortment_titles == ["Stół dębowy"]

    serialized = canonical_json(payload)
    assert "hidden@a.pl" not in serialized
    assert "https://a.pl/secret" not in serialized
    assert "evidence" not in payload.canonical_dict()
    assert "pages" not in payload.canonical_dict()


def test_broken_legal_name_is_omitted_for_olx() -> None:
    lead = _lead(
        source="olx",
        name="Damian Pokojski",
        legal_name=(
            "res odpowiedzialności Administratora i Właścicielem sklepu "
            "jest Damian Pokojski spółka z ograniczoną odpowiedzialnością"
        ),
    )
    payload = build_fact_payload(lead, "role")

    assert payload.company_name is None


def test_shopify_name_is_a_safe_fallback() -> None:
    lead = _lead(source="shopify", name="MISBHV", legal_name="fragment bez formy prawnej")
    payload = build_fact_payload(lead, "role")

    assert payload.company_name == "MISBHV"


def test_payload_without_enrichment_raises() -> None:
    lead = Lead(
        id=1,
        source="shopify",
        source_id="a.pl",
        name="Sklep",
        domain="a.pl",
        status="enriched",
    )

    with pytest.raises(ValueError, match="no enrichment"):
        build_fact_payload(lead, "role")


def test_context_hash_ignores_whitespace_but_tracks_content() -> None:
    payload = FactPayload(
        domain="a.pl",
        source="shopify",
        company_name="A sp. z o.o.",
        storefront_platform="shopify",
        description="Meble z drewna.",
        text_excerpt="Pierwsza linia\nDruga linia",
        headings=["Pracownia"],
        assortment_titles=["Stół"],
        contact_kind="role",
    )
    same = FactPayload(**{**payload.canonical_dict(), "description": "  Meble   z drewna. "})
    changed = FactPayload(**{**payload.canonical_dict(), "description": "Meble z metalu."})

    assert context_hash(payload) == context_hash(same)
    assert context_hash(payload) != context_hash(changed)


def test_insufficient_context_requires_prose_or_assortment() -> None:
    empty = FactPayload(
        domain="a.pl",
        source="shopify",
        company_name=None,
        storefront_platform=None,
        description="",
        text_excerpt="",
        headings=[],
        assortment_titles=[],
        contact_kind="role",
    )
    with_products = FactPayload(**{**empty.canonical_dict(), "assortment_titles": ["Stół"]})

    assert payload_is_insufficient(empty) is True
    assert payload_is_insufficient(with_products) is False
