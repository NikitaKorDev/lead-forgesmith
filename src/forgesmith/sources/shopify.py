"""Shopify PL stores.

Why Shopify first: the storefront exposes structured data through public
endpoints (``/products.json``), so we get a store's real product catalogue
instead of scraping markup. Merchants running custom storefronts or a pile of
manual back-office work are also the best fit for an automation pitch.
"""

from __future__ import annotations

import html as html_module
import re
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

import structlog

from forgesmith.domain import LeadCandidate, LeadSource, parse_domain
from forgesmith.sources.base import BaseSource
from forgesmith.utils import first_str

log = structlog.get_logger(__name__)

_STORE_MARKERS = ("cdn.shopify.com", "Shopify.theme", "shopify-features", "myshopify.com")
_SLUG_SPLIT = re.compile(r"[-_.]+")
_OG_SITE_NAME = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_SITE_NAME_REVERSED = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name["\']',
    re.IGNORECASE,
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TITLE_SPLIT = re.compile(r"\s+[|\u2013\u2014\u00b7\u2022]\s+|\s+-\s+")


def _clean_title(value: str) -> str:
    value = html_module.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _extract_site_name(html: str, domain: str = "") -> str | None:
    """Shop name, preferring og:site_name over the marketing-heavy <title>."""
    for pattern in (_OG_SITE_NAME, _OG_SITE_NAME_REVERSED):
        match = pattern.search(html)
        if match:
            name = _shorten(_clean_title(match.group(1)), domain)
            if name:
                return name

    match = _TITLE.search(html)
    if not match:
        return None
    return _shorten(_clean_title(match.group(1)), domain)


def _shorten(value: str, domain: str = "") -> str | None:
    """Pick the brand out of "kategoria | MARKA" using the domain as a hint."""
    if not value:
        return None
    parts = [part.strip() for part in _TITLE_SPLIT.split(value) if part.strip()]
    if len(parts) == 1:
        return value[:200] or None

    stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    if stem:
        for part in parts:
            if stem in re.sub(r"[^a-z0-9]", "", part.lower()):
                return part[:200]
    return parts[-1][:200]


def load_seeds(path: Path) -> list[str]:
    """Read one domain/URL per line; ``#`` starts a comment."""
    if not path.exists():
        raise FileNotFoundError(
            f"Shopify seed file not found: {path}. "
            "Create it with one store domain per line (e.g. example.pl)."
        )
    seeds: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            seeds.append(line)
    return seeds


def _pretty_name(domain: str) -> str:
    stem = domain.split(".")[0]
    return " ".join(part.capitalize() for part in _SLUG_SPLIT.split(stem) if part)


class ShopifySource(BaseSource):
    name = LeadSource.SHOPIFY

    def __init__(self, *args: object, seeds: Sequence[str], **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.seeds = list(seeds)
        self._homepage_cache: dict[str, str | None] = {}

    async def discover(self) -> AsyncIterator[LeadCandidate]:
        for seed in self.seeds:
            if not self._budget_left():
                return
            candidate = await self.inspect(seed)
            if candidate is None:
                log.debug("shopify.not_shopify", seed=seed)
                continue
            self._emitted += 1
            yield candidate

    async def inspect(self, seed: str) -> LeadCandidate | None:
        domain = (
            seed.strip().removeprefix("https://").removeprefix("http://").split("/")[0]
        ).lower()
        domain = domain.removeprefix("www.")
        if not domain:
            return None

        products = await self._products(domain)
        if products:
            return self._from_products(domain, products, await self._site_name_for(domain))

        collections = await self._collections(domain)
        if collections:
            return self._from_collections(domain, collections, await self._site_name_for(domain))

        home = await self._homepage(domain)
        if home is not None and any(marker in home for marker in _STORE_MARKERS):
            return LeadCandidate(
                source=self.name,
                source_id=domain,
                name=_extract_site_name(home, domain) or _pretty_name(domain),
                url=f"https://{domain}/",
                domain=domain,
                platform="shopify",
                raw={"detected_via": "html_marker", "products": []},
            )
        return None

    # ------------------------------------------------------------- internals
    async def _homepage(self, domain: str) -> str | None:
        if domain not in self._homepage_cache:
            response = await self.client.get(f"https://{domain}/")
            self._homepage_cache[domain] = response.text if response is not None else None
        return self._homepage_cache[domain]

    async def _site_name_for(self, domain: str) -> str | None:
        """Store name from og:site_name/<title>. Product vendor is usually the
        brand of the goods, not the shop we are writing to."""
        html = await self._homepage(domain)
        return _extract_site_name(html, domain) if html else None

    async def _products(self, domain: str) -> list[dict[str, Any]]:
        url = f"https://{domain}/products.json?limit={self.settings.shopify_products_limit}"
        payload = await self.client.get_json(url)
        if isinstance(payload, dict) and isinstance(payload.get("products"), list):
            return [p for p in payload["products"] if isinstance(p, dict)]
        return []

    async def _collections(self, domain: str) -> list[dict[str, Any]]:
        payload = await self.client.get_json(f"https://{domain}/collections.json?limit=50")
        if isinstance(payload, dict) and isinstance(payload.get("collections"), list):
            return [c for c in payload["collections"] if isinstance(c, dict)]
        return []

    def _from_products(
        self, domain: str, products: list[dict[str, Any]], site_name: str | None
    ) -> LeadCandidate:
        vendors = Counter(
            p["vendor"].strip()
            for p in products
            if isinstance(p.get("vendor"), str) and p["vendor"].strip()
        )
        types = Counter(
            p["product_type"].strip()
            for p in products
            if isinstance(p.get("product_type"), str) and p["product_type"].strip()
        )
        name = (
            site_name or (vendors.most_common(1)[0][0] if vendors else None) or _pretty_name(domain)
        )
        sample = [
            {
                "title": p.get("title"),
                "type": p.get("product_type") or None,
                "price": (p.get("variants") or [{}])[0].get("price") if p.get("variants") else None,
            }
            for p in products[:12]
        ]
        return LeadCandidate(
            source=self.name,
            source_id=domain,
            name=name,
            url=f"https://{domain}/",
            domain=domain,
            category=types.most_common(1)[0][0] if types else None,
            platform="shopify",
            raw={
                "detected_via": "products.json",
                "site_name": site_name,
                "product_count": len(products),
                "product_types": dict(types.most_common(5)),
                "products": sample,
            },
        )

    def _from_collections(
        self, domain: str, collections: list[dict[str, Any]], site_name: str | None
    ) -> LeadCandidate:
        titles = [t for c in collections if (t := first_str(c, "title", "handle"))]
        return LeadCandidate(
            source=self.name,
            source_id=domain,
            name=site_name or _pretty_name(domain),
            url=f"https://{domain}/",
            domain=domain,
            category=titles[0] if titles else None,
            platform="shopify",
            raw={
                "detected_via": "collections.json",
                "collections": titles[:12],
                "product_count": 0,
                "products": [],
            },
        )


def seeds_from_paths(paths: Iterable[Path]) -> list[str]:
    seeds: list[str] = []
    for path in paths:
        seeds.extend(load_seeds(path))
    seen: set[str] = set()
    unique: list[str] = []
    for seed in seeds:
        key = parse_domain(seed) or seed.lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique
