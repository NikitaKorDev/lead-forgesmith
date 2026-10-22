"""Persistence helpers. All writes go through here so dedupe rules stay in one place."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forgesmith.db.models import (
    Contact,
    Enrichment,
    EnrichmentStatusDB,
    Event,
    Lead,
    LeadStatusDB,
    Suppression,
    utcnow,
)
from forgesmith.domain import LeadCandidate, UpsertStats

if TYPE_CHECKING:
    from forgesmith.enrichment.models import EnrichmentResult

_CHUNK = 200
_MUTABLE_FIELDS = ("name", "url", "city", "category", "phone", "platform")


@dataclass(slots=True)
class SuppressionSet:
    emails: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)

    def blocks_email(self, email: str) -> bool:
        email = email.strip().lower()
        domain = email.rsplit("@", 1)[-1]
        return email in self.emails or domain in self.domains

    def blocks_domain(self, domain: str | None) -> bool:
        return bool(domain) and domain.strip().lower() in self.domains


@dataclass(slots=True)
class FinishOutcome:
    lead_status: str
    enrichment_status: str | None
    stored_contacts: int


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

    async def select_pending(
        self,
        *,
        limit: int | None = None,
        source: str | None = None,
        lead_id: int | None = None,
        force: bool = False,
        max_attempts: int = 3,
    ) -> Sequence[Lead]:
        """Leads eligible for enrichment, least-attempted first.

        Ordering by ``(attempts, id)`` keeps poison leads from permanently
        occupying the front of the queue and is portable between SQLite and
        Postgres (unlike NULL ordering on ``last_attempt_at``).
        """
        attempts = func.coalesce(Enrichment.attempts, 0)
        stmt = select(Lead).outerjoin(Enrichment, Enrichment.lead_id == Lead.id)
        if lead_id is not None:
            stmt = stmt.where(Lead.id == lead_id)
        if source:
            stmt = stmt.where(Lead.source == source)
        if force:
            stmt = stmt.where(
                Lead.status.in_([LeadStatusDB.NEW, LeadStatusDB.ENRICHED, LeadStatusDB.SKIPPED])
            )
        else:
            stmt = stmt.where(
                Lead.status == LeadStatusDB.NEW,
                attempts < max_attempts,
            )
        stmt = stmt.order_by(attempts, Lead.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def domains(self) -> dict[str, int]:
        """Map every non-null domain to its lead id (for duplicate detection)."""
        rows = await self.session.execute(
            select(Lead.domain, Lead.id).where(Lead.domain.is_not(None))
        )
        return {domain: lead_id for domain, lead_id in rows if domain}

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
        for name in _MUTABLE_FIELDS:
            new_value = getattr(cand, name)
            if new_value and getattr(lead, name) != new_value:
                setattr(lead, name, new_value)
                changed = True
        if cand.raw and cand.raw != lead.raw:
            lead.raw = cand.raw
            changed = True
        if changed:
            lead.updated_at = utcnow()
        return changed


class EnrichmentRepo:
    """Persistence for crawl outcomes, contacts and the audit event."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ read
    async def get(self, lead_id: int) -> Enrichment | None:
        stmt = select(Enrichment).where(Enrichment.lead_id == lead_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def load_suppressions(self) -> SuppressionSet:
        rows = await self.session.execute(select(Suppression.value, Suppression.kind))
        values = SuppressionSet()
        for value, kind in rows:
            value = value.strip().lower()
            if not value:
                continue
            if kind == "domain":
                values.domains.add(value)
            else:
                values.emails.add(value)
        return values

    async def list_contacts(
        self,
        *,
        limit: int = 50,
        lead_id: int | None = None,
    ) -> Sequence[tuple[Contact, Lead]]:
        stmt = (
            select(Contact, Lead)
            .join(Lead, Lead.id == Contact.lead_id)
            .order_by(Lead.id, Contact.confidence.desc(), Contact.id)
            .limit(limit)
        )
        if lead_id is not None:
            stmt = stmt.where(Contact.lead_id == lead_id)
        rows = await self.session.execute(stmt)
        return [(contact, lead) for contact, lead in rows]

    # ----------------------------------------------------------------- write
    async def save_resolution(
        self,
        lead_id: int,
        *,
        domain: str | None,
        source: str,
        evidence: dict,
    ) -> Enrichment:
        """Record a resolve-only result without touching lead status or domain."""
        enrichment = await self.get(lead_id)
        if enrichment is None:
            enrichment = Enrichment(lead_id=lead_id, status=None, attempts=0)
            self.session.add(enrichment)
        enrichment.domain = domain
        enrichment.domain_source = source if domain else "none"
        enrichment.evidence = {**(enrichment.evidence or {}), "resolution": evidence}
        enrichment.updated_at = utcnow()
        await self.session.flush()
        return enrichment

    async def claim_attempt(self, lead_id: int) -> int:
        """Increment attempts before network I/O so Ctrl-C still advances it."""
        enrichment = await self.get(lead_id)
        now = utcnow()
        if enrichment is None:
            enrichment = Enrichment(
                lead_id=lead_id,
                status=None,
                attempts=1,
                last_attempt_at=now,
            )
            self.session.add(enrichment)
        else:
            enrichment.attempts = (enrichment.attempts or 0) + 1
            enrichment.last_attempt_at = now
            enrichment.updated_at = now
        await self.session.flush()
        return enrichment.attempts

    async def finish(
        self,
        lead_id: int,
        result: EnrichmentResult,
        *,
        max_attempts: int = 3,
        email_limit: int = 3,
    ) -> FinishOutcome:
        lead = await self.session.get(Lead, lead_id)
        if lead is None:
            raise ValueError(f"lead {lead_id} does not exist")
        enrichment = await self.get(lead_id)
        if enrichment is None:
            enrichment = Enrichment(lead_id=lead_id, status=None, attempts=0)
            self.session.add(enrichment)
            await self.session.flush()

        prior_lead_status = lead.status
        prior_enrichment_status = enrichment.status
        attempts = enrichment.attempts or 0
        status = EnrichmentStatusDB(result.status)
        now = utcnow()

        # A forced retry must never take an already-enriched lead backwards.
        retain = prior_lead_status == LeadStatusDB.ENRICHED and status != EnrichmentStatusDB.OK

        if retain:
            lead.status = LeadStatusDB.ENRICHED
            enrichment.status = prior_enrichment_status or EnrichmentStatusDB.OK
            enrichment.last_error = result.last_error
            enrichment.evidence = {
                **(enrichment.evidence or {}),
                "last_retry": result.evidence,
                "force_retained": True,
            }
            stored = await self._contact_count(lead_id)
        else:
            enrichment.status = status
            enrichment.domain = result.domain
            enrichment.domain_source = result.domain_source
            enrichment.site_url = result.site_url
            enrichment.storefront_platform = result.storefront_platform
            enrichment.legal_name = result.legal_name
            enrichment.nip = result.nip
            enrichment.description = result.description
            enrichment.context = result.context
            enrichment.evidence = result.evidence
            enrichment.engine = result.engine
            enrichment.last_error = result.last_error
            if result.promote_domain and result.domain:
                lead.domain = result.domain

            stored = await self._store_contacts(
                lead_id,
                result,
                limit=email_limit,
            )
            if status == EnrichmentStatusDB.OK and stored == 0:
                # Every candidate was suppressed; there is nothing Phase 3 can use.
                status = EnrichmentStatusDB.NO_CONTACT
                enrichment.status = status

            if status == EnrichmentStatusDB.OK:
                lead.status = LeadStatusDB.ENRICHED
                enrichment.last_success_at = now
                enrichment.last_error = None
            elif status == EnrichmentStatusDB.NO_CONTACT:
                lead.status = LeadStatusDB.SKIPPED
                enrichment.last_success_at = now
            elif status in (EnrichmentStatusDB.NO_SITE, EnrichmentStatusDB.BLOCKED):
                lead.status = LeadStatusDB.SKIPPED
            else:
                lead.status = LeadStatusDB.SKIPPED if attempts >= max_attempts else LeadStatusDB.NEW

        enrichment.last_attempt_at = enrichment.last_attempt_at or now
        enrichment.updated_at = now

        event_detail = {
            "emails": stored,
            "platform": enrichment.storefront_platform,
            "reason": result.reason,
            "engine": enrichment.engine,
            "domain_source": enrichment.domain_source,
        }
        if retain:
            event_detail["retained"] = True
        self.session.add(
            Event(
                entity="lead",
                entity_id=lead_id,
                action="enriched" if status == EnrichmentStatusDB.OK else "skipped",
                detail=event_detail,
            )
        )
        await self.session.flush()
        return FinishOutcome(
            lead_status=lead.status,
            enrichment_status=enrichment.status,
            stored_contacts=stored,
        )

    # -------------------------------------------------------------- internals
    async def _contact_count(self, lead_id: int) -> int:
        stmt = select(func.count()).select_from(Contact).where(Contact.lead_id == lead_id)
        return (await self.session.execute(stmt)).scalar_one()

    async def _store_contacts(
        self,
        lead_id: int,
        result: EnrichmentResult,
        *,
        limit: int,
    ) -> int:
        if not result.contacts:
            return await self._contact_count(lead_id)

        suppressions = await self.load_suppressions()
        site_domain = (result.domain or "").lower()
        if suppressions.blocks_domain(site_domain):
            return await self._contact_count(lead_id)

        rows = (
            (await self.session.execute(select(Contact).where(Contact.lead_id == lead_id)))
            .scalars()
            .all()
        )
        existing = {contact.email.strip().lower(): contact for contact in rows}
        for hit in result.contacts:
            key = hit.email.strip().lower()
            if not key or suppressions.blocks_email(key):
                continue
            contact = existing.get(key)
            if contact is not None:
                contact.kind = hit.kind
                contact.confidence = hit.confidence
                contact.source_url = hit.source_url
                continue
            if len(existing) >= limit:
                break
            contact = Contact(
                lead_id=lead_id,
                email=hit.email.strip(),
                kind=hit.kind,
                source_url=hit.source_url,
                confidence=hit.confidence,
                verified_at=None,
            )
            self.session.add(contact)
            existing[key] = contact
        await self.session.flush()
        return len(existing)
