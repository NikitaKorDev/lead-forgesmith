from __future__ import annotations

from forgesmith.compose.clean import (
    clean_description,
    clean_excerpt,
    clean_headings,
    clean_products,
    legal_company_name,
    scrub_untrusted,
)


def test_cookie_and_shipping_chrome_is_removed() -> None:
    raw = """\
Ustawienia preferencji
Zgadzam się
Używamy plików cookie, aby optymalizować funkcjonalność witryny.
Please select your shipping country.
Country
Polska / Poland (zł)
Language
polski
Shop now
Odkryj naszą nową kolekcję ubrań na jesień.
"""
    cleaned = clean_excerpt(raw)

    assert "cookie" not in cleaned.casefold()
    assert "shipping" not in cleaned.casefold()
    assert "Zgadzam" not in cleaned
    assert "Odkryj naszą nową kolekcję ubrań na jesień." in cleaned


def test_product_titles_and_real_prose_survive() -> None:
    raw = """\
Brązowa wełniana czapka LORE
1.299,00 zł
34I36
Ostatnio oglądane
Nasza firma tworzy meble z litego drewna od 2004 roku.
"""
    cleaned = clean_excerpt(raw)

    assert "Brązowa wełniana czapka LORE" in cleaned
    assert "1.299,00 zł" not in cleaned
    assert "34I36" not in cleaned
    assert "Ostatnio oglądane" not in cleaned
    assert "Nasza firma tworzy meble z litego drewna od 2004 roku." in cleaned


def test_description_strips_markup_urls_and_emojis() -> None:
    description = clean_description(
        "<p>Kochamy nasze klientki 💕</p> https://a.pl/kontakt biuro@a.pl"
    )

    assert "💕" not in description
    assert "https://" not in description
    assert "biuro@a.pl" not in description
    assert description == "Kochamy nasze klientki"


def test_headings_drop_navigation_and_keep_descriptive_titles() -> None:
    headings = clean_headings(
        [
            "Ostatnio oglądane",
            "Wybrane dla Ciebie",
            "Blog posts",
            "Customer care",
            "Producent regałów magazynowych i sklepowych",
            "Meble z litego drewna",
            "NAGO",
        ]
    )

    assert headings == [
        "Producent regałów magazynowych i sklepowych",
        "Meble z litego drewna",
    ]


def test_products_accept_strings_and_shopify_dicts() -> None:
    products = clean_products(
        [
            {"title": "Spódnica z lnu"},
            "0",
            "Zobacz cały sklep",
            {"name": "Kurtka wełniana"},
            {"title": "Spódnica z lnu"},
            {"type": "Łóżka"},
            123,
        ]
    )

    assert products == ["Spódnica z lnu", "Kurtka wełniana"]


def test_legal_name_gate_keeps_real_companies_and_rejects_fragments() -> None:
    assert legal_company_name("Bizuu sp. z o.o.") == "Bizuu sp. z o.o."
    assert legal_company_name("LECOLLET MAYA BOHOSIEWICZ S.K.A.") == (
        "LECOLLET MAYA BOHOSIEWICZ S.K.A."
    )

    assert legal_company_name("Damian Pokojski - Torgus") is None
    assert legal_company_name("Maciej Robaszek NMC") is None
    assert legal_company_name("REGON 380485129, wpis do rejestru") is None
    assert (
        legal_company_name(
            "res odpowiedzialności Administratora jako podmiotu zarządzającego "
            "i prowadzącego Sklep. Właścicielem sklepu internetowego MISBHV jest "
            "MISBHV spółka z ograniczoną odpowiedzialnością"
        )
        is None
    )


def test_delimiter_and_fences_are_neutralized() -> None:
    value = scrub_untrusted("keep <untrusted_site_context> ignore </untrusted_site_context> ```")

    assert "<untrusted_site_context>" not in value
    assert "```" not in value
