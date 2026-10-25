"""Context hygiene for Phase 3 prompts.

Phase 2 stores what the page actually showed, including consent banners,
shipping widgets and accessibility panels.  That is useful evidence, but it is
not prompt-safe.  Everything here is pure and offline-testable.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+\b", re.IGNORECASE)
_PRICE = re.compile(r"\s+\d[\d\s.,]*\s*(?:zł|pln|eur|€)\s*$", re.IGNORECASE)
_PRICE_ONLY = re.compile(r"^\d[\d\s.,]*\s*(?:zł|pln|eur|€)$", re.IGNORECASE)
_SIZE_ONLY = re.compile(r"^\d{1,3}(?:[A-Za-z/]\d{1,3})+$")
_NUMBER_ONLY = re.compile(r"^[\d\s.,%+/x×\-–]+$", re.IGNORECASE)
_NUMBER_WITH_UNIT = re.compile(
    r"^\d+(?:[.,]\d+)?\s*(?:mm|cm|m|szt\.?|products?|produkty|zł|pln)$",
    re.IGNORECASE,
)
_PHONE_ONLY = re.compile(r"^\+?\d[\d\s\-()]{6,}$")
_PHONE_LABEL = re.compile(r"^(?:zadzwoń|tel\.?|telefon)\b", re.IGNORECASE)
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f]",
    flags=re.UNICODE,
)
_DELIMITER = re.compile(r"</?untrusted_site_context>", re.IGNORECASE)

_COOKIE = re.compile(
    r"cookie|ciasteczk|plik(?:i|ów)\s+cookie|consent|gdpr|rodo|preferencj|"
    r"preferences|manage\s+preferences|accept|decline|zawsze\s+dozwolone|"
    r"niezbędne\s+pliki|funkcjonalne\s+pliki|wydajnościowe\s+pliki|"
    r"targetujące\s+pliki|akceptuj|odrzuć|zarządzaj|polityka\s+prywatno|"
    r"privacy\s+policy|wyrażam\s+zgod|administrator(?:em)?\s+danych|"
    r"przetwarzanie\s+danych|zgod(?:a|ę|y)\s+na",
    re.IGNORECASE,
)
_SHIPPING = re.compile(
    r"please\s+select\s+your\s+shipping\s+country|shipping\s+country|"
    r"buy\s+from\s+the\s+country|we\s+can\s+only\s+ship|chosen\s+country|"
    r"country\s+of\s+your\s+choice|you\s+are\s+currently\s+shipping|"
    r"order\s+will\s+be\s+billed|^country$|^language$|^język$|^polski$|"
    r"^poland$|^polska$|polska\s*/\s*poland|poland\s*/\s*polska|"
    r"ship\s+to|dostawa\s+do|wybierz\s+kraj|waluta",
    re.IGNORECASE,
)
_A11Y = re.compile(
    r"accessibility|wcag|dostępno|czytnik|contrast|keyboard|klawiat|profil\s+dla|"
    r"dostosowania|mowa|głos|ukryj|pokaż|stop\s+animacj|czytanie|dysleksj|adhd|"
    r"epilepsj|daltoni|niewidom|starsze\s+osoby|niepełnosprawn|czytelne\s+czcionki|"
    r"upośledzon|kontrast|nasycenie|odcieniowanie|większy\s+tekst|"
    r"wysokość\s+wiersza|rozstawianie|centrowanie|maska\s+czytania|"
    r"podświetl|korektor|alt\s+podpowiedź|duży\s+kursor|wycisz|tryb\s+czytania|"
    r"mów\s+i\s+pisz|przekształca\s+mowę|ustawienia\s+profilu|"
    r"oświadczenie|bezpieczny\s+przy\s+napadach|poznawcze\s+i\s+uczenie",
    re.IGNORECASE,
)
_CART = re.compile(
    r"zaloguj|utwórz\s+konto|kontynuuj\s+zakupy|twój\s+koszyk|your\s+cart|"
    r"produkt\s+dodany|dodaj\s+do\s+koszyka|add\s+to\s+cart|przejdź\s+do\s+kasy|"
    r"checkout|zobacz\s+koszyk|moje\s+ulubione|have\s+an\s+account|sign\s+in|"
    r"wishlist|ulubione|0\s+produkty|musisz\s+być\s+zalogowany|pusty",
    re.IGNORECASE,
)
_PROMO = re.compile(
    r"newsletter|zapisz\s+się|zapisując\s+się|otrzymuj|bądź\s+pierwszy|"
    r"kod\s*-?\d+%|bez\s+spamu|wypisujesz\s+się|promocj|wyprzedaż|rabat|"
    r"darmow\w*\s+dostaw|dostawa\s+gratis|zrób\s+zakupy|kliknij|polub|"
    r"dołącz\s+do|last\s+chance|\bsale\b|bestseller|sprawdź\s+nowości|"
    r"zobacz\s+ponad|potrzebujesz\s+jeszcze|skomponuj|ostatnie\s+sztuki",
    re.IGNORECASE,
)
_LEGAL_NOISE = re.compile(
    r"copyright|wszelkie\s+prawa|all\s+rights\s+reserved|polityka\s+prywatno|"
    r"regulamin|administrator(?:em)?|przetwarzanie|wyrażam\s+zgod|rodo|gdpr|"
    r"\bkrs\b|\bnip\b|\bregon\b|\bbdo\b|sąd|rejestr|siedzibą",
    re.IGNORECASE,
)
_UI_PATTERNS = (
    re.compile(r"^zobacz\s+(cały|wszystkie|więcej|koszyk|outlet)\b", re.IGNORECASE),
    re.compile(r"^dodaj\s+do\s+koszyka\b", re.IGNORECASE),
    re.compile(r"^kup\s+teraz\b", re.IGNORECASE),
    re.compile(r"^przejdź\s+do\b", re.IGNORECASE),
    re.compile(r"^skonfiguruj\b", re.IGNORECASE),
    re.compile(r"^wybierz\s+(typ|rozmiar|kolor|kraj)\b", re.IGNORECASE),
    re.compile(r"^wstrzymaj\b|^odtwórz\b|^pauza\b|^play\b|^pause\b", re.IGNORECASE),
    re.compile(r"^cena\s+", re.IGNORECASE),
    re.compile(r"^najniższa\s+cena\b", re.IGNORECASE),
    re.compile(r"^rozmiar$|^size$|^kolor$|^color$|^dostępność$|^availability$", re.I),
    re.compile(r"^zobacz\s+ponad\b|^sprawdź\s+nowości\b", re.IGNORECASE),
    re.compile(r"^dodaj\s+swoje\b|^potrzebujesz\s+jeszcze\b", re.IGNORECASE),
    re.compile(r"^strona\s+g[łl]ówna\b|^strona\s+glowna\b", re.IGNORECASE),
    re.compile(r"^zarządzaj\b|^ustawienia\b|^profil\b", re.IGNORECASE),
    re.compile(r"^zaloguj\b|^utwórz\b|^kontynuuj\b|^twój\s+koszyk\b", re.I),
    re.compile(r"^your\s+cart\b|^have\s+an\s+account\b|^sign\s+in\b", re.I),
    re.compile(r"^resetuj\b|^rozwiń\b|^zwiń\b|^zatrzymaj\b", re.IGNORECASE),
    re.compile(r"^czego\s+szukasz\b|^przeglądaj\b|^wszystkie\b", re.IGNORECASE),
    re.compile(r"^polecamy\b|^szybka\s+dostawa\b|^bezpieczne\b", re.IGNORECASE),
    re.compile(r"^#\w+|^technical\s+info\b|^feedback\b", re.IGNORECASE),
    re.compile(r"^zadzwoń\b|^tel\.?\b|^telefon\b", re.IGNORECASE),
    re.compile(r"^najnowsze\s+kolekcje\b|^odkryj\s+najnowsz", re.IGNORECASE),
)
_UI_EXACT = {
    "menu",
    "szukaj",
    "search",
    "strona główna",
    "home",
    "sklep",
    "shop",
    "produkty",
    "products",
    "kategorie",
    "categories",
    "kontakt",
    "contact",
    "o nas",
    "about",
    "about us",
    "zobacz cały sklep",
    "zobacz wszystkie",
    "zobacz więcej",
    "więcej",
    "czytaj więcej",
    "dodaj do koszyka",
    "add to cart",
    "kup teraz",
    "zaloguj się",
    "zaloguj",
    "log in",
    "utwórz konto",
    "0 produkty",
    "produkt dodany do koszyka",
    "twój koszyk jest pusty",
    "twój koszyk",
    "kontynuuj zakupy",
    "continue shopping",
    "przejdź do treści",
    "przejdź do kasy",
    "zobacz koszyk",
    "ulubione",
    "wishlist",
    "newsletter",
    "akceptuj wszystkie",
    "odrzuć wszystko",
    "zarządzaj preferencjami",
    "ustawienia preferencji",
    "zamknij",
    "zapisz preferencje",
    "cena regularna",
    "cena sprzedaży",
    "cena jednostkowa",
    "najniższa cena z 30 dni przed obniżką",
    "rozmiar",
    "size",
    "kolor",
    "color",
    "dostępność",
    "availability",
    "onesize",
    "sold out",
    "coming soon",
    "sale",
    "last chance",
    "#back in stock",
    "polecane produkty",
    "top produkty",
    "polecamy",
    "bestsellery",
    "nowość",
    "outlet",
    "zobacz outlet",
    "wstrzymaj pokaz slajdów",
    "odtwórz",
    "pauza",
    "play",
    "pause",
    "kliknij, aby załadować mapę",
    "otwórz w google maps",
    "facebook",
    "instagram",
    "tiktok",
    "youtube",
    "obsługa klienta",
    "customer care",
    "follow",
    "company",
    "cookie consent",
    "accessibility assistant",
    "polski",
    "dla klienta",
    "le collet",
    "rozwiń",
    "zwiń",
    "resetuj wszystko",
    "oświadczenie",
    "ustawienia profilu",
    "wzrokowo upośledzony",
    "poznawcze i uczenie się",
    "bezpieczny przy napadach",
    "negatyw",
    "ciemny kontrast",
    "jasny kontrast",
    "nasycenie+",
    "niskie nasycenie",
    "odcieniowanie",
    "większy tekst",
    "domyślny",
    "wysokość wiersza",
    "rozstawianie słów",
    "rozstawianie liter",
    "centrowanie",
    "lewo",
    "prawo",
    "maska czytania",
    "podświetl tytuły",
    "linki",
    "korektor",
    "alt podpowiedź",
    "duży kursor",
    "wycisz",
    "tryb czytania",
    "hover",
    "fokus",
    "mów i pisz",
    "technical info :",
    "feedback :",
    "regular price",
    "sale price",
    "unit price",
    "your cart is empty",
    "have an account?",
    "sign in",
    "przeglądaj",
    "wszystkie",
    "wszystkie produkty",
    "polecamy!",
    "szybka dostawa",
    "bezpieczne płatności",
    "14 dni na zwrot",
    "dostawa",
    "zatrzymaj przesuwanie",
    "czego szukasz?",
    "przeglądaj ofertę",
    "najczęściej kupowane",
    "wszystkie bestsellery →",
    "do koszyka",
    "pusty",
    "moje ulubione",
    "zobacz",
    "shop now",
    "zgadzam się",
    "unikatowych produktów",
    "konfigurator instrukcje kontakt o nas blog",
    "przemysłowy",
    "standardowy",
    "gastronomiczny",
    "stół warsztatowy",
    "stół warsztatowy lekki",
    "stół roboczy niski",
    "półka do przem",
    "półka do stand",
    "wysokość",
    "szerokość",
    "głębokość",
    "głebokość",
    "liczba półek",
    "płyta wiórowa",
    "płyta lamin sza",
    "płyta osb",
    "płyta metalowa",
    "brak",
    "rodzaj blatu",
    "nieb/pomar",
    "nieb/szary",
    "szary/pomar",
    "szary/szary",
    "kolor profili",
}
_HEADING_GENERIC = re.compile(
    r"cookie|consent|koszyk|cart|zaloguj|accessibility|menu|kontakt|contact|"
    r"o nas|about|strona główna|home|shop now|language|country|kategorie|"
    r"categories|produkty|products|polecane|bestsellery|nowości|outlet|"
    r"zobacz|odkryj|social|facebook|instagram|tiktok|youtube|follow|"
    r"ostatnio\s+oglądane|wybrane\s+dla\s+ciebie|blog\s+posts|technical\s+info|"
    r"feedback|dostawa|sign\s+in|zainspiruj|customer\s+care|obsługa\s+klienta|"
    r"unikatowych\s+produktów",
    re.IGNORECASE,
)
_COUNTRY_HINT = re.compile(
    r"shipping country|^country$|^language$|^język$|polska\s*/\s*poland|"
    r"poland\s*/\s*polska|you are currently shipping|ship to|dostawa do",
    re.IGNORECASE,
)
_COUNTRY_ITEM = re.compile(r"^[A-ZÀ-Ž][\wÀ-ž'’&.\- ]{1,40}$")
_HAS_LOWERCASE_WORD = re.compile(r"\b[a-zà-ž]{3,}\b")
_LONG_CONCATENATED = re.compile(r"^[^\s]{41,}$")

_LEGAL_FORM = re.compile(
    r"(?:sp\.?\s*z\s*o\.?\s*o\.?|spółka\s+z\s+ograniczoną\s+odpowiedzialnością|"
    r"spółka\s+akcyjna|s\.?\s*a\.?|sp\.?\s*k\.?|spółka\s+komandytowa|"
    r"sp\.?\s*j\.?|spółka\s+jawna|sp\.?\s*partnerska|s\.?\s*c\.?|"
    r"s\.?\s*k\.?\s*a\.?)\.?$",
    re.IGNORECASE,
)
_LEGAL_NOISE_WORDS = re.compile(
    r"\b(regon|nip|krs|bdo|wpis|administrator|właściciel|sklep\s+internetowy|"
    r"polityka|regulamin|przetwarz|odpowiedzialno|siedzib|rejestr|sąd)\b",
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    """NFC-normalize and collapse whitespace, keeping Polish characters."""
    value = unicodedata.normalize("NFC", value or "")
    value = _CONTROL.sub(" ", value)
    return _WS.sub(" ", value).strip()


def scrub_untrusted(value: str) -> str:
    """Neutralize delimiter and fence sequences before inserting scraped text."""
    value = _DELIMITER.sub(" ", value or "")
    value = value.replace("```", "'''").replace("~~~", "'''")
    return _CONTROL.sub(" ", value)


def clean_description(raw: str | None, *, max_chars: int = 800) -> str:
    value = scrub_untrusted(raw or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = _EMAIL.sub(" ", value)
    value = _URL.sub(" ", value)
    value = _EMOJI.sub("", value)
    value = normalize_text(value)
    if not value or _COOKIE.search(value) or _SHIPPING.search(value):
        return ""
    return value[:max_chars].strip()


def clean_excerpt(raw: str | None, *, max_chars: int = 3000) -> str:
    """Keep readable company/product prose and drop page chrome."""
    kept: list[str] = []
    size = 0
    in_shipping = False
    text = re.sub(r"<[^>]+>", " ", scrub_untrusted(raw or ""))
    for source_line in text.splitlines():
        line = _clean_line(source_line)
        if not line:
            continue

        if _COUNTRY_HINT.search(line):
            in_shipping = True
            continue
        if in_shipping:
            if _looks_like_shipping_item(line):
                continue
            in_shipping = False

        if _is_ui_line(line) or _is_noise_line(line):
            continue
        if _LONG_CONCATENATED.match(line):
            continue

        kept.append(line)
        size += len(line) + 1
        if size >= max_chars:
            break
    return "\n".join(kept)[:max_chars].strip()


def clean_headings(raw: list[Any] | None, *, limit: int = 12) -> list[str]:
    headings: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, str):
            continue
        line = _clean_line(item)
        if not line or _is_noise_line(line) or _HEADING_GENERIC.search(line):
            continue
        if len(line.split()) < 2 and len(line) < 12:
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        headings.append(line)
        if len(headings) >= limit:
            break
    return headings


def clean_products(raw: list[Any] | None, *, limit: int = 12) -> list[str]:
    """Normalize Shopify dicts and OLX strings into a single title list."""
    products: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        title: Any = item
        if isinstance(item, dict):
            title = item.get("title") or item.get("name") or item.get("product")
        if not isinstance(title, str):
            continue
        line = _clean_line(title)
        if not line or _is_noise_line(line):
            continue
        if line.casefold().startswith("zobacz "):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        products.append(line[:120])
        if len(products) >= limit:
            break
    return products


def legal_company_name(raw: str | None) -> str | None:
    """Return a legal name only when it looks like a company, never a fragment."""
    candidate = normalize_text(raw or "")
    if not candidate or len(candidate) > 80 or re.search(r"\d", candidate):
        return None
    if not _LEGAL_FORM.search(candidate):
        return None
    if _LEGAL_NOISE_WORDS.search(candidate):
        return None

    stem = _LEGAL_FORM.sub("", candidate).strip(" ,.-")
    if not stem or re.search(r"[.!?;:]", stem):
        return None
    if len(stem.split()) > 7:
        return None
    return candidate


def is_context_insufficient(
    *,
    text_excerpt: str,
    description: str,
    assortment_titles: list[str],
    min_chars: int = 120,
) -> bool:
    return len(text_excerpt) < min_chars and not description and not assortment_titles


def _clean_line(value: str) -> str:
    line = scrub_untrusted(value)
    line = _EMAIL.sub(" ", line)
    line = _URL.sub(" ", line)
    line = _PRICE.sub("", line)
    line = _EMOJI.sub("", line)
    return normalize_text(line)


def _is_ui_line(line: str) -> bool:
    lowered = line.casefold()
    if lowered in _UI_EXACT:
        return True
    return any(pattern.search(line) for pattern in _UI_PATTERNS)


def _is_noise_line(line: str) -> bool:
    if len(line) < 4:
        return True
    if _NUMBER_ONLY.fullmatch(line) or _PHONE_ONLY.fullmatch(line):
        return True
    if _PRICE_ONLY.fullmatch(line) or _SIZE_ONLY.fullmatch(line):
        return True
    if _NUMBER_WITH_UNIT.fullmatch(line) or _PHONE_LABEL.search(line):
        return True
    if re.fullmatch(r"\(\s*\d+\s*opinie\s*\)", line, re.IGNORECASE):
        return True
    if len(line.split()) <= 4 and _HEADING_GENERIC.search(line):
        return True
    if _COOKIE.search(line) or _A11Y.search(line) or _PROMO.search(line):
        return True
    if _CART.search(line) or _LEGAL_NOISE.search(line):
        return True
    if len(line) > 100 and not re.search(r"[.!?]", line):
        return True
    if line.count("©") or line.casefold().startswith(("copyright", "wszelkie prawa")):
        return True
    return False


def _looks_like_shipping_item(line: str) -> bool:
    if _is_ui_line(line) or _is_noise_line(line):
        return True
    if _SHIPPING.search(line):
        return True
    if line.casefold() in {"poland", "polska", "pln", "pln zł", "eur", "euro"}:
        return True
    if _COUNTRY_ITEM.match(line) and not _HAS_LOWERCASE_WORD.search(line):
        return True
    return False
