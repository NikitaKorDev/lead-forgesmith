"""Resolve a lead to a storefront domain using evidence, never guesses.

Order: the source-provided domain (OLX already maps ``externalUrl`` into it),
then a human override file, then the OLX shop page, then (opt-in) the offer
page. No search-engine fallback: a wrong domain produces a confident pitch to
the wrong company, which is worse than an explicit ``no_site``.
"""

from __future__ import annotations

import csv
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import structlog

from forgesmith.domain import parse_domain, registrable_domain, same_site
from forgesmith.enrichment.fetch import SiteFetcher
from forgesmith.enrichment.site import parse_html

log = structlog.get_logger(__name__)

_BLOCKED_DOMAINS = {
    "olx.pl",
    "olxgroup.com",
    "olxcdn.com",
    "olx.org",
    "olx.bg",
    "olx.ro",
    "olx.ua",
    "olx.pt",
    "otodom.pl",
    "otomoto.pl",
    "obido.pl",
    "allegro.pl",
    "ceneo.pl",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "google.com",
    "apple.com",
    "goo.gl",
    "linktr.ee",
    "bit.ly",
    "tinyurl.com",
    "wikipedia.org",
}
_BLOCKED_SUFFIXES = (
    ".olx.pl",
    ".olxstatic.com",
    ".olxcdn.com",
    ".olx.org",
    ".olxgroup.com",
    ".facebook.com",
    ".instagram.com",
    ".google.com",
    ".gstatic.com",
    ".apple.com",
    ".cloudfront.net",
    ".googleusercontent.com",
)
_WEBSITE_HINTS = ("strona", "www", "sklep", "website", "odwiedź", "odwiedz", "zobacz")


@dataclass(slots=True)
class ResolvedDomain:
    domain: str
    source: str
    site_url: str
    evidence: dict = field(default_factory=dict)


def load_overrides(path: Path) -> dict[int, str]:
    """Read ``lead_id,domain,note`` overrides; a missing file means no overrides."""
    if not path.exists():
        return {}
    overrides: dict[int, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_id = (row.get("lead_id") or "").strip()
            raw_domain = (row.get("domain") or "").strip()
            if not raw_id.isdigit() or not raw_domain:
                continue
            domain = parse_domain(raw_domain)
            if domain and _usable_host(domain):
                overrides[int(raw_id)] = domain
    return overrides


def _usable_host(host: str | None) -> bool:
    if not host or "." not in host:
        return False
    host = host.lower().removeprefix("www.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    base = registrable_domain(host)
    if host in _BLOCKED_DOMAINS or (base and base in _BLOCKED_DOMAINS):
        return False
    return not host.endswith(_BLOCKED_SUFFIXES)


def extract_external_domain(html: str, page_url: str, *, own_domain: str | None) -> str | None:
    """Pick the most likely storefront link on an OLX shop/offer page."""
    page = parse_html(html, page_url)
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, link in enumerate(page.links):
        href = link.href.strip()
        if not href.startswith(("http://", "https://", "//")):
            continue
        absolute = urljoin(page_url, href)
        host = parse_domain(absolute)
        if not host or host in seen or not _usable_host(host):
            continue
        if own_domain and same_site(host, own_domain):
            continue
        if host == (urlsplit(page_url).hostname or "").lower():
            continue
        seen.add(host)
        haystack = f"{absolute} {link.text}".lower()
        score = 0 if any(hint in haystack for hint in _WEBSITE_HINTS) else 1
        ranked.append((score, order, host))
    ranked.sort()
    return ranked[0][2] if ranked else None


async def resolve_domain(
    lead: object,
    fetcher: SiteFetcher,
    overrides: dict[int, str] | None = None,
    *,
    deep: bool = False,
    deadline: float | None = None,
) -> ResolvedDomain | None:
    """Return a domain with its provenance, or ``None`` when nothing is usable."""
    lead_id = getattr(lead, "id", 0)
    domain = getattr(lead, "domain", None)
    if domain and _usable_host(domain):
        return ResolvedDomain(
            domain=domain,
            source="source",
            site_url=f"https://{domain}/",
            evidence={"method": "source", "detail": "lead.domain"},
        )

    overrides = overrides or {}
    if lead_id in overrides:
        domain = overrides[lead_id]
        return ResolvedDomain(
            domain=domain,
            source="override",
            site_url=f"https://{domain}/",
            evidence={"method": "override", "detail": "data/domain_overrides.csv"},
        )

    raw = getattr(lead, "raw", None) or {}
    ad = raw.get("ad") if isinstance(raw, dict) else None
    if isinstance(ad, dict):
        subdomain = ad.get("shop_subdomain")
        if isinstance(subdomain, str) and subdomain.strip():
            shop_url = f"https://{subdomain.strip().removeprefix('www.')}.olx.pl/home/?tab=contact"
            snapshot = await fetcher.fetch(shop_url, deadline=deadline)
            if snapshot.ok:
                domain = extract_external_domain(
                    snapshot.html, snapshot.final_url, own_domain="olx.pl"
                )
                if domain:
                    return ResolvedDomain(
                        domain=domain,
                        source="shop_page",
                        site_url=f"https://{domain}/",
                        evidence={
                            "method": "shop_page",
                            "checked": shop_url,
                            "final_url": snapshot.final_url,
                        },
                    )

    offer_url = getattr(lead, "url", None)
    if deep and isinstance(offer_url, str) and offer_url.startswith(("http://", "https://")):
        snapshot = await fetcher.fetch(offer_url, deadline=deadline)
        if snapshot.ok:
            domain = extract_external_domain(snapshot.html, snapshot.final_url, own_domain="olx.pl")
            if domain:
                return ResolvedDomain(
                    domain=domain,
                    source="offer_page",
                    site_url=f"https://{domain}/",
                    evidence={
                        "method": "offer_page",
                        "checked": offer_url,
                        "final_url": snapshot.final_url,
                    },
                )

    log.debug("resolve.no_site", lead_id=lead_id, url=offer_url)
    return None
