"""Persistence helpers. All writes go through here so dedupe rules stay in one place."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forgesmith.db.models import Lead, LeadStatusDB, utcnow
from forgesmith.domain import LeadCandidate, UpsertStats

_CHUNK = 200
_MUTABLE_FIELDS = ("name", "url", "city", "category", "phone", "platform")


class LeadRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ read
    async def by_source_id(self, source: str, source_id: str) -> Lead | None:
        stmt = select(Lead).where(Lead.source == source, Lead.source_id == source_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def by_domain(self, domain: str) -> Lead | None:
        stmt = select(Lead).where(Lead.domain == domain).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        source: str | None = None,
    ) -> Sequence[Lead]:
        stmt = select(Lead).order_by(Lead.id.desc()).limit(limit).offset(offset)
        if status:
            stmt = stmt.where(Lead.status == status)
        if source:
            stmt = stmt.where(Lead.source == source)
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self) -> int:
        return (await self.session.execute(select(func.count()).select_from(Lead))).scalar_one()

    async def status_counts(self) -> dict[str, int]:
        stmt = select(Lead.status, func.count()).group_by(Lead.status)
        return {row[0]: row[1] for row in await self.session.execute(stmt)}

    async def source_counts(self) -> dict[str, int]:
        stmt = select(Lead.source, func.count()).group_by(Lead.source)
        return {row[0]: row[1] for row in await self.session.execute(stmt)}

    # ----------------------------------------------------------------- write
    async def upsert_many(self, candidates: Iterable[LeadCandidate]) -> UpsertStats:
        """Insert new leads, refresh untouched ones, skip duplicate domains.

        Duplicate rule: one lead per domain. A second marketplace offer pointing
        at a domain we already track is recorded as a duplicate rather than
        creating a second outbound target.
        """
        stats = UpsertStats()
        batch = list(candidates)
        for start in range(0, len(batch), _CHUNK):
            stats += await self._upsert_chunk(batch[start : start + _CHUNK])
        return stats

    async def _upsert_chunk(self, chunk: list[LeadCandidate]) -> UpsertStats:
        stats = UpsertStats()
        if not chunk:
            return stats

        keys = {(c.source.value, c.source_id) for c in chunk}
        existing_stmt = select(Lead).where(Lead.source.in_({k[0] for k in keys}))
        existing = {
            (lead.source, lead.source_id): lead
            for lead in (await self.session.execute(existing_stmt)).scalars()
            if (lead.source, lead.source_id) in keys
        }

        domains = {c.domain for c in chunk if c.domain}
        domain_map: dict[str, Lead] = {}
        if domains:
            stmt = select(Lead).where(Lead.domain.in_(domains))
            domain_map = {
                lead.domain: lead
                for lead in (await self.session.execute(stmt)).scalars()
                if lead.domain
            }

        for cand in chunk:
            key = (cand.source.value, cand.source_id)
            lead = existing.get(key)
            if lead is not None:
                if lead.status == LeadStatusDB.NEW and self._refresh(lead, cand):
                    stats.updated += 1
                continue

            if cand.domain and cand.domain in domain_map:
                stats.duplicates += 1
                continue

            lead = Lead(
                source=cand.source.value,
                source_id=cand.source_id,
                name=cand.name,
                url=cand.url,
                domain=cand.domain,
                city=cand.city,
                category=cand.category,
                phone=cand.phone,
                platform=cand.platform,
                raw=cand.raw,
                status=LeadStatusDB.NEW,
            )
            self.session.add(lead)
            existing[key] = lead
            if cand.domain:
                domain_map[cand.domain] = lead
            stats.inserted += 1

        await self.session.flush()
        return stats

    @staticmethod
    def _refresh(lead: Lead, cand: LeadCandidate) -> bool:
        changed = False
        for field in _MUTABLE_FIELDS:
            new_value = getattr(cand, field)
            if new_value and getattr(lead, field) != new_value:
                setattr(lead, field, new_value)
                changed = True
        if cand.raw and cand.raw != lead.raw:
            lead.raw = cand.raw
            changed = True
        if changed:
            lead.updated_at = utcnow()
        return changed
