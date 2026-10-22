"""Phase 2: resolve, fetch, extract and persist storefront enrichment."""

from __future__ import annotations

from typing import Any

from forgesmith.enrichment.models import (
    EmailHit,
    EnrichmentResult,
    FetchResult,
    PageSnapshot,
)

__all__ = [
    "EmailHit",
    "EnrichmentResult",
    "FetchResult",
    "PageSnapshot",
    "enrich_pending",
]


async def enrich_pending(*args: Any, **kwargs: Any) -> Any:
    """Public entry point; imported lazily to keep ``http`` free of cycles."""
    from forgesmith.enrichment.runner import enrich_pending as _impl

    return await _impl(*args, **kwargs)
