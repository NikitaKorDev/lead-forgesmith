"""HTML understanding for enrichment: text, links, platform, page classification.

Everything here is pure and offline-testable. ``utils.strip_tags`` is not used:
it leaves ``<script>``/``<style>`` contents in the text, which is how filenames
and JS fragments turn into fake emails.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from forgesmith.domain import registrable_domain, same_site

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
_LAYOUT_TAGS = {"nav", "header", "footer", "aside"}
_CONSENT_MARKERS = ("cookie", "consent", "gdpr", "rodo")
_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_HEADING_TAGS = {"h1", "h2"}
_FILE_SUFFIX = re.compile(
    r"\.(?:pdf|docx?|xlsx?|csv|jpe?g|png|gif|webp|svg|ico|css|js|json|xml|zip|rar|7z|mp4|mp3|webm|avi)$",
    re.IGNORECASE,
)
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{2,}")
_SAFE_LINK = re.compile(r"^(?:https?:)?//|^/", re.IGNORECASE)
_SKIP_LINK = re.compile(
    r"(?:^|/)(?:koszyk|cart|checkout|logowanie|login|konto|account|rejestracja|register|"
    r"wishlist|ulubione|search|szukaj|blog|feed|wp-json|tag|author|page|strona|pagination)"
    r"(?:/|$|\?|#)",
    re.IGNORECASE,
)
_LINK_TIERS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (
        1,
        re.compile(r"kontakt|contact|napisz|skontaktuj|write-to-us", re.IGNORECASE),
    ),
    (
        2,
        re.compile(r"o-nas|o-firmie|o firmie|about|firma|historia|o_mnie", re.IGNORECASE),
    ),
    (
        3,
        re.compile(
            r"regulamin|polityka[-_ ]?prywatnosci|privacy|rodo|dane[-_ ]?firmy|terms",
            re.IGNORECASE,
        ),
    ),
    (
        4,
        re.compile(
            r"sklep|produkty|produkt|oferta|cennik|shop|products|collections", re.IGNORECASE
        ),
    ),
)

_PLATFORM_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("woocommerce", ("wp-content/plugins/woocommerce", "woocommerce-page", "wc-ajax")),
    ("shopify", ("cdn.shopify.com", "myshopify.com", "shopify.theme", "shopify-features")),
    ("prestashop", ("prestashop", "/modules/ps_", "var prestashop")),
    ("shoper", ("shoper.pl", "shoper.com", "shoper")),
    ("idosell", ("idosell", "idosec")),
    ("wix", ("wixstatic.com", "wix.com", "x-wix-request-id")),
    ("squarespace", ("squarespace.com", "static1.squarespace.com")),
    ("magento", ("magento", "mage/cookies", "mage-init")),
)
_PARKED_MARKERS = (
    "domena na sprzedaz",
    "domena na sprzedaż",
    "buy this domain",
    "this domain is parked",
    "domain is for sale",
    "parkingcrew",
    "aftermarket.pl",
    "sedo.com",
    "domeny.pl",
)
_CHALLENGE_MARKERS = (
    "cf-challenge",
    "just a moment",
    "attention required",
    "datadome",
    "captcha",
    "checking your browser",
    "access denied",
)


@dataclass(slots=True)
class Link:
    href: str
    text: str = ""


@dataclass(slots=True)
class ParsedPage:
    url: str
    text: str
    main_text: str
    headings: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    mailtos: list[str] = field(default_factory=list)
    jsonld: list[Any] = field(default_factory=list)
    description: str | None = None
    title: str | None = None


class _PageParser(HTMLParser):
    def __init__(self, *, skip_layout: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_layout = skip_layout
        self._text: list[str] = []
        self._main: list[str] = []
        self._headings: list[str] = []
        self._links: list[Link] = []
        self._mailtos: list[str] = []
        self._jsonld: list[Any] = []
        self._description: str | None = None
        self._title_parts: list[str] = []
        self._in_title = False
        self._skip_stack: list[str] = []
        self._layout_stack: list[str] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._heading: str | None = None
        self._heading_text: list[str] = []
        self._jsonld_chunks: list[str] | None = None

    # ------------------------------------------------------------- lifecycle
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "script" and "ld+json" in values.get("type", "").lower():
            self._jsonld_chunks = []
            return
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            if name in ("description", "og:description") and values.get("content"):
                self._description = values["content"].strip()[:1000]
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP_TAGS or _is_hidden(values):
            if tag not in _VOID_TAGS:
                self._skip_stack.append(tag)
            return
        if self.skip_layout and _is_consent(values):
            if tag not in _VOID_TAGS:
                self._layout_stack.append(tag)
            return
        if tag in _LAYOUT_TAGS:
            self._layout_stack.append(tag)
        if tag in _BLOCK_TAGS:
            self._append("\n")
        if tag == "br":
            self._append("\n")
            return
        if tag == "a" and values.get("href"):
            self._link_href = values["href"].strip()
            self._link_text = []
            if self._link_href.lower().startswith("mailto:"):
                self._mailtos.append(self._link_href)
        if tag in _HEADING_TAGS:
            self._heading = tag
            self._heading_text = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self._jsonld_chunks is not None:
            self._finish_jsonld()
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._append("\n")
        if self._skip_stack and tag == self._skip_stack[-1]:
            self._skip_stack.pop()
            return
        if self._layout_stack and tag == self._layout_stack[-1]:
            self._layout_stack.pop()
        if tag == "a" and self._link_href is not None:
            text = _clean(" ".join(self._link_text))
            self._links.append(Link(href=self._link_href, text=text))
            self._link_href = None
            self._link_text = []
        if tag == self._heading:
            heading = _clean(" ".join(self._heading_text))
            if heading:
                self._headings.append(heading)
            self._heading = None
            self._heading_text = []

    def handle_data(self, data: str) -> None:
        if self._jsonld_chunks is not None:
            self._jsonld_chunks.append(data)
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._append(data)
        if self._link_href is not None:
            self._link_text.append(data)
        if self._heading is not None:
            self._heading_text.append(data)

    # --------------------------------------------------------------- helpers
    def _is_skipping(self) -> bool:
        return bool(self._skip_stack) or (self.skip_layout and bool(self._layout_stack))

    def _append(self, text: str) -> None:
        if not text or self._skip_stack:
            return
        self._text.append(text)
        if not (self.skip_layout and self._layout_stack):
            self._main.append(text)

    def _finish_jsonld(self) -> None:
        raw = "".join(self._jsonld_chunks or []).strip()
        self._jsonld_chunks = None
        if not raw:
            return
        try:
            self._jsonld.append(json.loads(raw))
        except ValueError:
            return

    def result(self, url: str) -> ParsedPage:
        return ParsedPage(
            url=url,
            text=_clean_text("".join(self._text)),
            main_text=_clean_text("".join(self._main)),
            headings=_dedupe(self._headings),
            links=self._links,
            mailtos=self._mailtos,
            jsonld=self._jsonld,
            description=self._description,
            title=_clean(" ".join(self._title_parts)) or None,
        )


def _is_hidden(attrs: dict[str, str]) -> bool:
    if "hidden" in attrs:
        return True
    if attrs.get("type", "").lower() == "hidden":
        return True
    if attrs.get("aria-hidden", "").lower() == "true":
        return True
    style = attrs.get("style", "").lower().replace(" ", "")
    return "display:none" in style or "visibility:hidden" in style


def _is_consent(attrs: dict[str, str]) -> bool:
    marker = f"{attrs.get('class', '')} {attrs.get('id', '')}".lower()
    return any(value in marker for value in _CONSENT_MARKERS)


def _clean(value: str) -> str:
    return _WS.sub(" ", value).strip()


def _clean_text(value: str) -> str:
    lines = [_clean(line) for line in value.splitlines()]
    return _BLANK_LINES.sub("\n", "\n".join(line for line in lines if line)).strip()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = value.casefold()
        if key and key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def parse_html(html: str, url: str, *, skip_layout: bool = False) -> ParsedPage:
    parser = _PageParser(skip_layout=skip_layout)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient; keep a partial page
        pass
    return parser.result(url)


def visible_text(html: str, url: str) -> str:
    """Visible body text including header/footer (contact data usually lives there)."""
    return parse_html(html, url, skip_layout=False).text


def main_excerpt(pages: list[tuple[str, ParsedPage]], max_chars: int) -> str:
    """Join cleaned main text from home/about pages, dedupe lines, then truncate."""
    seen: set[str] = set()
    kept: list[str] = []
    length = 0
    for _url, page in pages:
        for line in page.main_text.splitlines():
            line = _clean(line)
            if len(line) < 4:
                continue
            key = line.casefold()
            if key in seen:
                continue
            seen.add(key)
            if length + len(line) + 1 > max_chars:
                return "\n".join(kept)[:max_chars].strip()
            kept.append(line)
            length += len(line) + 1
    return "\n".join(kept).strip()


def rank_links(home_url: str, page: ParsedPage, *, limit: int = 4) -> list[str]:
    """Return same-site links worth spending a page budget on, best first."""
    home = registrable_domain(home_url)
    if not home:
        return []
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, link in enumerate(page.links):
        href = link.href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if not _SAFE_LINK.match(href):
            continue
        absolute = urldefrag(urljoin(home_url, href))[0]
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if registrable_domain(parts.hostname or "") != home:
            continue
        path = parts.path.rstrip("/") or "/"
        if path == "/":
            continue
        if _FILE_SUFFIX.search(path) or _SKIP_LINK.search(path):
            continue
        key = path.casefold()
        if key in seen:
            continue
        seen.add(key)
        haystack = f"{parts.path} {link.text}"
        tier = next((tier for tier, pattern in _LINK_TIERS if pattern.search(haystack)), None)
        if tier is None:
            continue
        ranked.append((tier, order, absolute))
    ranked.sort()
    unique: list[str] = []
    seen_urls: set[str] = set()
    for _tier, _order, url in ranked:
        if url not in seen_urls:
            seen_urls.add(url)
            unique.append(url)
        if len(unique) >= limit:
            break
    return unique


_PRODUCT_LINK = re.compile(r"produkt|product|sklep|shop|kolekcj|collection|oferta", re.IGNORECASE)
_GENERIC_ANCHORS = {
    "sklep",
    "produkty",
    "produkt",
    "oferta",
    "cennik",
    "shop",
    "products",
    "collections",
    "zobacz",
    "zobacz więcej",
    "więcej",
    "wiecej",
}


def product_titles(page: ParsedPage, *, limit: int = 12) -> list[str]:
    """Cheap product-title fallback for non-Shopify sites (homepage links only)."""
    seen: set[str] = set()
    titles: list[str] = []
    for link in page.links:
        text = _clean(link.text)
        if not text or len(text) > 120 or text.casefold() in _GENERIC_ANCHORS:
            continue
        if not _PRODUCT_LINK.search(link.href):
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        titles.append(text)
        if len(titles) >= limit:
            break
    return titles


def detect_platform(html: str, headers: dict[str, str] | None = None) -> str | None:
    haystack = html.lower()
    header_text = " ".join(f"{key}:{value}" for key, value in (headers or {}).items()).lower()
    for name, markers in _PLATFORM_MARKERS:
        if any(marker in haystack or marker in header_text for marker in markers):
            return name
    return None


def is_challenge_page(snapshot: Any) -> bool:
    status = getattr(snapshot, "status", None)
    if status in (401, 403, 429, 503):
        return True
    html = (getattr(snapshot, "html", "") or "").lower()
    return any(marker in html for marker in _CHALLENGE_MARKERS)


def is_parked_page(html: str, text: str) -> bool:
    haystack = f"{html[:5000]}\n{text}".lower()
    if any(marker in haystack for marker in _PARKED_MARKERS):
        return True
    return len(text.strip()) < 80 and "domain" in haystack


def is_js_shell(snapshot: Any) -> bool:
    html = (getattr(snapshot, "html", "") or "").lower()
    text = visible_text(getattr(snapshot, "html", "") or "", getattr(snapshot, "final_url", ""))
    return len(text) < 200 and any(
        marker in html for marker in ('id="root"', "id='root'", "__next_data__", "wixstatic")
    )


def same_registrable(first: str, second: str) -> bool:
    return same_site(first, second)
