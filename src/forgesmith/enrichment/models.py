"""Value objects shared by the enrichment stages.

Kept free of I/O and ORM imports (except the status enum) so ``http.py`` can
import :class:`FetchResult` without pulling in the crawler.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forgesmith.db.models import EnrichmentStatusDB


@dataclass(slots=True)
class RunStats:
    """Counters for one CLI run; printed as the final summary table."""

    scanned: int = 0
    resolved: int = 0
    overridden: int = 0
    enriched: int = 0
    skipped: int = 0
    no_site: int = 0
    no_contact: int = 0
    blocked: int = 0
    failed: int = 0
    duplicates: int = 0
    emails: int = 0
    duration_s: float = 0.0


@dataclass(slots=True)
class FetchResult:
    """Normalised response from either PoliteClient or BrowserFetcher."""

    url: str
    final_url: str
    status: int | None
    html: str
    headers: dict[str, str] = field(default_factory=dict)
    engine: str = "http"
    error: str | None = None
    bytes: int = 0
    elapsed_ms: int = 0


@dataclass(slots=True)
class PageSnapshot:
    """A fetched page after robots/SSRF/engine decisions."""

    url: str
    final_url: str
    status: int | None
    html: str
    engine: str
    bytes: int
    elapsed_ms: int
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400


@dataclass(slots=True)
class EmailHit:
    """An eligible address observed verbatim on one of the lead's pages."""

    email: str
    kind: str
    confidence: float
    source_url: str
    local: str = ""
    domain: str = ""


@dataclass(slots=True)
class EnrichmentResult:
    """Everything one lead attempt contributes to the database."""

    status: EnrichmentStatusDB
    domain: str | None = None
    domain_source: str = "none"
    site_url: str | None = None
    storefront_platform: str | None = None
    legal_name: str | None = None
    nip: str | None = None
    description: str | None = None
    context: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    contacts: list[EmailHit] = field(default_factory=list)
    engine: str | None = None
    promote_domain: bool = False
    reason: str | None = None
    last_error: str | None = None
