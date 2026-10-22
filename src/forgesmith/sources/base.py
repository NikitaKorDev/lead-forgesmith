"""Source interface: everything that can produce LeadCandidates."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Protocol

import structlog

from forgesmith.config import Settings
from forgesmith.config import settings as default_settings
from forgesmith.domain import LeadCandidate, LeadSource

log = structlog.get_logger(__name__)


class Fetcher(Protocol):
    """Anything that can return pages: PoliteClient (httpx) or BrowserFetcher."""

    async def get(self, url: str, **kwargs: object) -> Any: ...

    async def get_json(self, url: str, **kwargs: object) -> object | None: ...


class BaseSource(ABC):
    name: LeadSource

    def __init__(
        self,
        client: Fetcher,
        settings: Settings | None = None,
        *,
        limit: int | None = None,
    ) -> None:
        self.client = client
        self.settings = settings or default_settings
        self.limit = limit
        self._emitted = 0

    def _budget_left(self) -> bool:
        return self.limit is None or self._emitted < self.limit

    @abstractmethod
    def discover(self) -> AsyncIterator[LeadCandidate]:
        """Yield candidates one at a time so a crash keeps earlier progress."""
        raise NotImplementedError
