"""Allegro.pl — intentionally disabled.

Allegro fronts its listings with DataDome and its ToS forbids scraping, so a
crawler here would be both fragile and a legal liability. It keeps the source
interface so it can be enabled later via the official REST API with a
registered application, or by dropping in a licensed data feed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import structlog

from forgesmith.domain import LeadCandidate, LeadSource
from forgesmith.sources.base import BaseSource

log = structlog.get_logger(__name__)


class AllegroSource(BaseSource):
    name = LeadSource.ALLEGRO

    async def discover(self) -> AsyncIterator[LeadCandidate]:  # pragma: no cover
        log.warning("allegro.disabled")
        raise NotImplementedError(
            "Allegro scraping is disabled by design. Use --source shopify/olx, "
            "or implement the official API integration first."
        )
        yield  # pragma: no cover - makes this an async generator
