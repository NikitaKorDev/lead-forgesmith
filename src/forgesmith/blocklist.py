"""Hosts that must never become an outbound target.

Social platforms, marketplaces, link shorteners and CDNs are the most common
"website" a source hands us: a Facebook page is often the only web presence a
small business has, and Overture's ``websites`` column is full of them. Mailing
one is either impossible or a guaranteed complaint, so the list lives here once
and both the enrichment resolver and the local-extract sources share it.
"""

from __future__ import annotations

import ipaddress

from forgesmith.domain import registrable_domain

BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
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
)
BLOCKED_SUFFIXES: tuple[str, ...] = (
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


def usable_host(host: str | None) -> bool:
    """True when a bare host is a plausible company storefront."""
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
    if host in BLOCKED_DOMAINS or (base and base in BLOCKED_DOMAINS):
        return False
    return not host.endswith(BLOCKED_SUFFIXES)
