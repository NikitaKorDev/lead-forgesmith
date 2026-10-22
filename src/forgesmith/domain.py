"""Domain types shared by every stage of the pipeline."""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

_TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid|gclid|yclid|msclkid|mc_eid|ref$|source$|_ga)", re.IGNORECASE
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
