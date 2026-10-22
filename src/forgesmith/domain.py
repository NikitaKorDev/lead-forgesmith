"""Domain types shared by every stage of the pipeline."""

from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

_TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid|gclid|yclid|msclkid|mc_eid|ref$|source$|_ga)", re.IGNORECASE
)

# Built-in two-label suffixes. A downloaded public-suffix list (tldextract)
# would make offline tests depend on the network, so the list stays local.
_MULTI_LABEL_SUFFIXES = frozenset(
    {
        # Poland: functional and the common city domains.
        "com.pl",
        "net.pl",
        "org.pl",
        "shop.pl",
        "biz.pl",
        "info.pl",
        "edu.pl",
        "gov.pl",
        "mil.pl",
        "nom.pl",
        "art.pl",
        "waw.pl",
        "gd.pl",
        "krakow.pl",
        "wroclaw.pl",
        "poznan.pl",
        "lodz.pl",
        "katowice.pl",
        "gda.pl",
        "sopot.pl",
        "szczecin.pl",
        "lublin.pl",
        "rzeszow.pl",
        "bialystok.pl",
        "bydgoszcz.pl",
        "torun.pl",
        "kielce.pl",
        "olsztyn.pl",
        "czest.pl",
        "slupsk.pl",
        "tychy.pl",
        "gliwice.pl",
        "zabrze.pl",
        "bytom.pl",
        "rybnik.pl",
        "radom.pl",
        "plock.pl",
        "elblag.pl",
        "wloclawek.pl",
        "tarnow.pl",
        "koszalin.pl",
        "legnica.pl",
        "zielonagora.pl",
        "opole.pl",
        # Common non-Polish multi-label suffixes seen in the dataset.
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "com.br",
        "co.nz",
        "co.za",
        "com.tr",
        "com.ua",
        "co.jp",
        "co.kr",
        "co.in",
        "co.il",
        "com.cn",
        "com.sg",
        "com.hk",
        "com.tw",
        "com.mx",
        "com.ar",
        "com.es",
        "com.de",
        "com.fr",
        "com.it",
        "com.nl",
        "com.se",
        "com.no",
        "com.dk",
        "com.fi",
        "com.cz",
        "com.sk",
        "com.hu",
        "com.ro",
        "com.bg",
        "com.hr",
        "com.rs",
        "com.by",
        "com.kz",
        "com.lt",
        "com.lv",
        "com.ee",
    }
)


class LeadSource(StrEnum):
    SHOPIFY = "shopify"
    OLX = "olx"
    ALLEGRO = "allegro"
    MANUAL = "manual"


class LeadStatus(StrEnum):
    NEW = "new"
    ENRICHED = "enriched"
    COMPOSED = "composed"
    QUEUED = "queued"
    SENT = "sent"
    REPLIED = "replied"
    BOUNCED = "bounced"
    SUPPRESSED = "suppressed"
    SKIPPED = "skipped"


def normalize_url(url: str) -> str:
    """Lowercase the host, drop tracking params and the fragment."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return url
    query = "&".join(
        piece
        for piece in parts.query.split("&")
        if piece and not _TRACKING_PARAMS.match(piece.split("=", 1)[0])
    )
    netloc = host if not parts.port else f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, query, ""))


def parse_domain(value: str) -> str | None:
    """Extract a bare host (no www, no port) from a URL or a raw domain."""
    value = value.strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value.split("/")[0]
    host = urlsplit(value).hostname or ""
    host = host.lower().removeprefix("www.")
    if "." not in host:
        return None
    return host


def registrable_domain(value: str) -> str | None:
    """Return the registrable part of a host: ``sklep.example.com.pl`` → ``example.com.pl``.

    IP literals are returned unchanged; they have no registrable suffix.
    """
    host = parse_domain(value)
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host

    labels = host.split(".")
    if len(labels) < 2:
        return host
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(first: str, second: str) -> bool:
    """True when two hosts belong to the same registrable domain."""
    left = registrable_domain(first)
    right = registrable_domain(second)
    return left is not None and left == right


class LeadCandidate(BaseModel):
    """A prospect as produced by a source, before any enrichment."""

    source: LeadSource
    source_id: str
    name: str
    url: str | None = None
    domain: str | None = None
    city: str | None = None
    category: str | None = None
    phone: str | None = None
    platform: str | None = None
    raw: dict = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _clean_url(cls, value: str | None) -> str | None:
        return normalize_url(value) if value else value

    @field_validator("domain")
    @classmethod
    def _clean_domain(cls, value: str | None) -> str | None:
        return parse_domain(value) if value else value

    @classmethod
    def from_site(
        cls, *, source: LeadSource, source_id: str, name: str, site_url: str, **kwargs: object
    ) -> LeadCandidate:
        """Build a candidate whose domain is derived from its own storefront URL."""
        return cls(
            source=source,
            source_id=source_id,
            name=name,
            url=site_url,
            domain=parse_domain(site_url),
            **kwargs,  # type: ignore[arg-type]
        )


class UpsertStats(BaseModel):
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0

    def __add__(self, other: UpsertStats) -> UpsertStats:
        return UpsertStats(
            inserted=self.inserted + other.inserted,
            updated=self.updated + other.updated,
            duplicates=self.duplicates + other.duplicates,
        )
