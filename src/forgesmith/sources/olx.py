"""OLX.pl offers.

Two strategies, in order of reliability:
1. The JSON blob OLX embeds in every listing page (survives markup redesigns).
2. Anchor scraping as a fallback when the blob is renamed.

An offer page is not the outbound target: enrichment resolves the seller to
their own storefront, because OLX contact goes through a form.

Terms of service: OLX restricts automated access. This adapter is opt-in and
kept for local research only; check the current OLX terms or use the official
API before running it. It does not bypass authentication, a paywall or a block.

URL shapes verified against production:
    /oferty/warszawa/q-meble/            query + city
    /nieruchomosci/mieszkania/wynajem/warszawa/   category path + city
    ...?page=2                            pagination
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import quote

import structlog

from forgesmith.domain import LeadCandidate, LeadSource
from forgesmith.sources.base import BaseSource
from forgesmith.utils import json_payloads, slugify, walk_dicts

log = structlog.get_logger(__name__)

_JSON_MARKERS = (
    "__PRERENDERED_STATE__",
    "__NEXT_DATA__",
    "__INITIAL_STATE__",
    "__NUXT__",
)
_OFFER_HREF = re.compile(r'href="(/d/oferta/[^"?#]+)"')
_OFFER_ID = re.compile(r"-ID([0-9A-Za-z]+)\.html$")
_ANCHOR_TITLE = re.compile(r'title="([^"]+)"')


class OlxSource(BaseSource):
    name = LeadSource.OLX

    def __init__(
        self,
        *args: object,
        queries: Sequence[str] = ("meble",),
        categories: Sequence[str] = (),
        cities: Sequence[str] = (),
        max_pages: int | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.queries = [q.strip() for q in queries if q.strip()]
        self.categories = [c.strip("/") for c in categories if c.strip("/")]
        self.cities = [c.strip("/") for c in cities if c.strip("/")]
        self.max_pages = max_pages or self.settings.olx_max_pages

    def listing_urls(self) -> list[str]:
        base = self.settings.olx_base_url.rstrip("/")
        cities = self.cities or [""]
        paths: list[str] = []
        for category in self.categories:
            for city in cities:
                parts = [part for part in (category, city) if part]
                paths.append("/" + "/".join(parts) + "/")
        for query in self.queries:
            slug = slugify(query)
            for city in cities:
                parts = ["oferty", *([city] if city else []), f"q-{quote(slug)}"]
                paths.append("/" + "/".join(parts) + "/")

        urls: list[str] = []
        for path in paths:
            for page in range(1, self.max_pages + 1):
                suffix = f"?page={page}" if page > 1 else ""
                urls.append(f"{base}{path}{suffix}")
        return urls

    async def discover(self) -> AsyncIterator[LeadCandidate]:
        log.warning(
            "olx.terms_review",
            detail=(
                "OLX restricts automated access; verify the current terms or use the "
                "official API before relying on this source"
            ),
        )
        seen: set[str] = set()
        for listing_url in self.listing_urls():
            if not self._budget_left():
                return
            response = await self.client.get(listing_url)
            if response is None:
                log.warning("olx.listing_failed", url=listing_url)
                continue
            for candidate in self.parse_listing(
                response.text, category=self._category_of(listing_url)
            ):
                if candidate.source_id in seen:
                    continue
                seen.add(candidate.source_id)
                self._emitted += 1
                yield candidate
                if not self._budget_left():
                    return

    # ------------------------------------------------------------- internals
    def _category_of(self, listing_url: str) -> str | None:
        for category in self.categories:
            if f"/{category}" in listing_url:
                return category
        for query in self.queries:
            if slugify(query) in listing_url:
                return query
        return None

    def parse_listing(self, html: str, *, category: str | None = None) -> list[LeadCandidate]:
        offers = self._from_json(html) or self._from_anchors(html)
        candidates: list[LeadCandidate] = []
        seen: set[str] = set()
        for offer in offers:
            candidate = self._to_candidate(offer, category)
            if candidate is None or candidate.source_id in seen:
                continue
            seen.add(candidate.source_id)
            candidates.append(candidate)
        return candidates

    def _from_json(self, html: str) -> list[dict[str, Any]]:
        offers: list[dict[str, Any]] = []
        for payload in json_payloads(html, _JSON_MARKERS):
            for node in walk_dicts(payload):
                url = node.get("url")
                if not isinstance(url, str) or "/d/oferta/" not in url:
                    continue
                title = node.get("title") or node.get("name")
                if not isinstance(title, str) or not title.strip():
                    continue
                offers.append(node)
        return offers

    def _from_anchors(self, html: str) -> list[dict[str, Any]]:
        offers: list[dict[str, Any]] = []
        for match in _OFFER_HREF.finditer(html):
            href = match.group(1)
            window = html[match.end() : match.end() + 400]
            title_match = _ANCHOR_TITLE.search(window)
            title = title_match.group(1) if title_match else href.rstrip("/").split("/")[-1]
            offers.append({"url": href, "title": title})
        return offers

    def _to_candidate(self, offer: dict[str, Any], category: str | None) -> LeadCandidate | None:
        raw_url = str(offer.get("url") or "")
        if not raw_url:
            return None
        if raw_url.startswith("/"):
            raw_url = f"{self.settings.olx_base_url.rstrip('/')}{raw_url}"

        contact = offer.get("contact") if isinstance(offer.get("contact"), dict) else {}
        shop = offer.get("shop") if isinstance(offer.get("shop"), dict) else {}
        is_business = bool(offer.get("isBusiness")) or bool(shop.get("subdomain"))

        # B2B only: private sellers have no company inbox to pitch and would
        # put the sending domain at risk.
        if not is_business:
            return None

        offer_id = str(offer.get("id") or "").strip()
        user = offer.get("user") if isinstance(offer.get("user"), dict) else {}
        seller_id = str(user.get("id") or "").strip()
        # One lead per seller, not per ad: we write to the business, and a shop
        # often posts dozens of near-identical listings.
        if not seller_id:
            id_match = _OFFER_ID.search(raw_url)
            seller_id = (
                id_match.group(1)
                if id_match
                else (offer_id or raw_url.rstrip("/").rsplit("/", 1)[-1])
            )

        title = str(offer.get("title") or offer.get("name") or "").strip()
        seller = str(contact.get("name") or user.get("name") or "").strip()
        name = seller or title
        if not name:
            return None

        location = offer.get("location") if isinstance(offer.get("location"), dict) else {}
        city = location.get("cityName")
        nested_city = location.get("city")
        if not city and isinstance(nested_city, dict):
            city = nested_city.get("name")
        if not city:
            mapped = offer.get("mapDetails")
            if isinstance(mapped, dict):
                city = mapped.get("cityName")

        external_url = offer.get("externalUrl")
        if isinstance(external_url, dict):
            external_url = external_url.get("url")

        price = offer.get("price") if isinstance(offer.get("price"), dict) else {}
        return LeadCandidate(
            source=self.name,
            source_id=f"olx:{seller_id}",
            name=name,
            url=raw_url,
            domain=str(external_url) if external_url else None,
            city=city,
            country="PL",
            category=category,
            phone=None,
            platform="olx",
            raw={
                "ad": {
                    "id": offer_id,
                    "title": title,
                    "url": raw_url,
                    "is_business": is_business,
                    "price": price.get("displayValue"),
                    "seller": seller,
                    "city": city,
                    "region": location.get("regionName"),
                    "external_url": external_url,
                    "shop_subdomain": shop.get("subdomain"),
                    "has_phone": bool(contact.get("phone")),
                }
            },
        )
