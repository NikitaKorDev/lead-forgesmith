from __future__ import annotations

from forgesmith.enrichment.site import (
    detect_platform,
    is_parked_page,
    main_excerpt,
    parse_html,
    product_titles,
    rank_links,
    visible_text,
)

HTML = """
<html><head>
<title>Drewno i Spółka | Meble</title>
<meta name="description" content="Meble dębowe z Warszawy.">
<script>var fake = "script@example.com";</script>
<script type="application/ld+json">{"@type": "Organization", "legalName": "Drewno i Spółka Sp. z o.o."}</script>
<style>.x { color: red }</style>
</head><body>
<header><a href="/">Home</a><a href="mailto:header@a.pl">mail</a></header>
<nav><a href="/kontakt">Kontakt</a></nav>
<main>
<h1>Meble dębowe</h1>
<h2>Robimy stoły</h2>
<p>Drewno i Spółka to rodzinna pracownia.</p>
<p>Produkujemy meble z litego drewna od 1998 roku.</p>
<div style="display:none">honeypot@a.pl</div>
<a href="/o-nas">O nas</a>
<a href="/kontakt">Napisz do nas</a>
<a href="/regulamin">Regulamin</a>
<a href="/sklep">Sklep</a>
<a href="/koszyk">Koszyk</a>
<a href="/blog">Blog</a>
<a href="/cennik.pdf">Cennik PDF</a>
<a href="https://facebook.com/firma">Facebook</a>
<a href="https://other.pl">Other</a>
<a href="mailto:kontakt@a.pl?subject=hi">Napisz</a>
</main>
<footer><p>NIP 5261040828</p><a href="/kontakt">Kontakt</a></footer>
</body></html>
"""


def test_visible_text_skips_scripts_styles_and_hidden_elements() -> None:
    text = visible_text(HTML, "https://a.pl/")
    assert "script@example.com" not in text
    assert ".x { color" not in text
    assert "honeypot@a.pl" not in text
    assert "Meble dębowe" in text
    assert "Drewno i Spółka" in text


def test_main_text_drops_layout_chrome() -> None:
    page = parse_html(HTML, "https://a.pl/", skip_layout=True)
    assert "Kontakt" not in page.main_text or "Napisz" in page.main_text
    assert "header@a.pl" not in page.main_text
    assert "Produkujemy meble" in page.main_text


def test_parser_collects_metadata_links_headings_and_jsonld() -> None:
    page = parse_html(HTML, "https://a.pl/")
    assert page.title == "Drewno i Spółka | Meble"
    assert page.description == "Meble dębowe z Warszawy."
    assert page.headings == ["Meble dębowe", "Robimy stoły"]
    assert any(link.href.startswith("mailto:kontakt@a.pl") for link in page.links)
    assert page.jsonld and page.jsonld[0]["legalName"].startswith("Drewno")


def test_rank_links_orders_contact_about_legal_products_and_skips_noise() -> None:
    page = parse_html(HTML, "https://a.pl/")
    ranked = rank_links("https://a.pl/", page, limit=4)
    assert ranked[0] == "https://a.pl/kontakt"
    assert "https://a.pl/o-nas" in ranked
    assert "https://a.pl/regulamin" in ranked
    assert all("koszyk" not in url and "blog" not in url for url in ranked)
    assert all("facebook.com" not in url and "other.pl" not in url for url in ranked)
    assert all(not url.endswith(".pdf") for url in ranked)


def test_main_excerpt_dedupes_truncates_and_keeps_about_text() -> None:
    page = parse_html(HTML, "https://a.pl/", skip_layout=True)
    excerpt = main_excerpt([("https://a.pl/", page)], max_chars=120)
    assert len(excerpt) <= 120
    assert "Produkujemy meble" in excerpt
    assert "Drewno i Spółka" in excerpt


def test_detect_platform_from_markers_and_headers() -> None:
    assert detect_platform("<link href='https://cdn.shopify.com/x'>") == "shopify"
    assert detect_platform("<div class='wp-content/plugins/woocommerce'>") == "woocommerce"
    assert detect_platform("plain", {"x-powered-by": "PrestaShop"}) == "prestashop"
    assert detect_platform("plain") is None


def test_product_titles_fall_back_to_homepage_links() -> None:
    page = parse_html(
        '<a href="/produkt/stol-debowy">Stół dębowy</a>'
        '<a href="/produkty">Produkty</a>'
        '<a href="/produkt/lozko">Łóżko</a>',
        "https://a.pl/",
    )
    assert product_titles(page) == ["Stół dębowy", "Łóżko"]


def test_parked_page_detection() -> None:
    assert is_parked_page("Domena na sprzedaż", "Domena na sprzedaż")
    assert not is_parked_page("<html>Sklep z meblami</html>", "Sklep z meblami")
