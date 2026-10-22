from __future__ import annotations

from forgesmith.enrichment.identity import find_legal_name, find_nip, normalize_nip, valid_nip
from forgesmith.enrichment.site import parse_html


def pages(*html: str) -> list[tuple[str, object]]:
    return [
        (f"https://a.pl/{index}", parse_html(value, f"https://a.pl/{index}"))
        for index, value in enumerate(html)
    ]


def test_valid_nip_checksum_and_normalization() -> None:
    assert valid_nip("5261040828")
    assert valid_nip("526-104-08-28")
    assert valid_nip("PL 5261040828")
    assert not valid_nip("1234567890")
    assert normalize_nip("PL 526-104-08-28") == "5261040828"


def test_find_nip_requires_a_valid_labelled_value() -> None:
    found = find_nip(pages("<p>NIP: 526-104-08-28</p>"))
    assert found is not None
    assert found["value"] == "5261040828"
    assert "526" in found["snippet"]

    assert find_nip(pages("<p>NIP: 123-456-78-90</p>")) is None
    assert find_nip(pages("<p>5261040828</p>")) is None


def test_legal_name_only_next_to_valid_nip_or_in_jsonld() -> None:
    text = "Drewno i Spółka Sp. z o.o. NIP 5261040828, ul. Leśna 1"
    found = find_nip(pages(text))
    assert found is not None
    name = find_legal_name(pages(text), found["value"])
    assert name is not None
    assert name.startswith("Drewno i Spółka")
    assert name.endswith("Sp. z o.o.")


def test_legal_name_from_jsonld_organization_only() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@type": "Organization", "legalName": "Meble Kowalski S.A."}'
        "</script>"
    )
    assert find_legal_name(pages(html), None) == "Meble Kowalski S.A."

    ignored = (
        '<script type="application/ld+json">'
        '{"@type": "Person", "legalName": "Nie Firma S.A."}'
        "</script>"
    )
    assert find_legal_name(pages(ignored), None) is None


def test_legal_name_must_be_within_eighty_chars_of_nip() -> None:
    filler = "x" * 120
    far = f"Drewno i Spółka Sp. z o.o. {filler} NIP 5261040828"
    found = find_nip(pages(far))
    assert found is not None
    assert find_legal_name(pages(far), found["value"]) is None


def test_lead_name_is_never_used_as_legal_name() -> None:
    assert find_legal_name(pages("<p>Maciej, Warszawa</p>"), None) is None
    assert find_legal_name(pages("<p>Maciej, Warszawa</p>"), "5261040828") is None
