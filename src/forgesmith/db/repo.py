"""Persistence helpers. All writes go through here so dedupe rules stay in one place."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from forgesmith.db.models import (
    Contact,
    Enrichment,
    EnrichmentStatusDB,
    Event,
    InboundMessage,
    InboundStatusDB,
    Lead,
    LeadStatusDB,
    MailboxState,
    Message,
    Send,
    Suppression,
    utcnow,
)
from forgesmith.domain import LeadCandidate, UpsertStats, is_freemail, registrable_domain
from forgesmith.eligibility import Eligibility, LeadState, evaluate
from forgesmith.inbox.models import InboundKind, ParsedInbound, RawMessage
from forgesmith.inbox.parse import canonical_message_id
from forgesmith.mailer.models import (
    ATTEMPT_STATUSES,
    LIVE_STATUSES,
    TERMINAL_LEAD_STATUSES,
    ApprovalRefused,
    QueuedMessage,
    RecheckReason,
    RecheckResult,
    Reservation,
    SendStatus,
)
from forgesmith.mailer.policy import ensure_utc, retry_not_before

if TYPE_CHECKING:
    from forgesmith.enrichment.models import EnrichmentResult

_CHUNK = 200
_MUTABLE_FIELDS = ("name", "url", "city", "category", "phone", "platform")

#: Lead lifecycle is monotonic: a late writer must never erase a stronger
#: outcome. Phase 4.5's inbox is a second writer, so a reply that lands between
#: ``select_queued`` and ``finalize`` would otherwise be overwritten with
#: ``sent``. ``suppressed`` always wins because it is a legal opt-out.
_LEAD_RANK: dict[str, int] = {
    LeadStatusDB.NEW: 0,
    LeadStatusDB.ENRICHED: 1,
    LeadStatusDB.COMPOSED: 2,
    LeadStatusDB.QUEUED: 3,
    LeadStatusDB.SENT: 4,
    LeadStatusDB.BOUNCED: 5,
    LeadStatusDB.REPLIED: 6,
    LeadStatusDB.SUPPRESSED: 7,
}


def advance_lead_status(lead: Lead, target: str) -> bool:
    """Move a lead forward only. Returns ``True`` when the status changed."""
    if _LEAD_RANK.get(target, -1) <= _LEAD_RANK.get(lead.status, -1):
        return False
    lead.status = target
    return True


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


@dataclass(frozen=True, slots=True)
class _LeadFacts:
    """The two lead columns :func:`forgesmith.eligibility.evaluate` reads."""

    status: str
    domain: str | None


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

    async def select_composable(
        self,
        *,
        limit: int | None = None,
        source: str | None = None,
        lead_id: int | None = None,
        regenerate: bool = False,
        retry_failed: bool = False,
        max_attempts: int = 3,
    ) -> Sequence[Lead]:
        """Leads with a role contact and a successful enrichment.

        Ordering by failed ``compose_failed`` events keeps one poison lead from
        permanently occupying the front of the queue, mirroring Phase 2.
        """
        stmt, order = self._composable_stmt(
            source=source,
            lead_id=lead_id,
            regenerate=regenerate,
            retry_failed=retry_failed,
            max_attempts=max_attempts,
        )
        stmt = (
            stmt.options(selectinload(Lead.contacts), selectinload(Lead.enrichment))
            .order_by(*order)
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().unique().all()

    async def count_composable(
        self,
        *,
        source: str | None = None,
        lead_id: int | None = None,
        regenerate: bool = False,
        retry_failed: bool = False,
        max_attempts: int = 3,
    ) -> int:
        """How many leads ``select_composable`` would return, without loading them."""
        stmt, _ = self._composable_stmt(
            source=source,
            lead_id=lead_id,
            regenerate=regenerate,
            retry_failed=retry_failed,
            max_attempts=max_attempts,
        )
        count = select(func.count()).select_from(stmt.subquery())
        return int((await self.session.execute(count)).scalar_one())

    def _composable_stmt(
        self,
        *,
        source: str | None,
        lead_id: int | None,
        regenerate: bool,
        retry_failed: bool,
        max_attempts: int,
    ) -> tuple[Any, tuple[Any, ...]]:
        """The selection rule shared by the runner and the ``--check`` count."""
        failed = (
            select(Event.entity_id.label("lead_id"), func.count().label("failed_attempts"))
            .where(Event.entity == "lead", Event.action == "compose_failed")
            .group_by(Event.entity_id)
            .subquery()
        )
        statuses = [LeadStatusDB.ENRICHED]
        if regenerate:
            statuses.append(LeadStatusDB.COMPOSED)

        stmt = (
            select(Lead)
            .join(Enrichment, Enrichment.lead_id == Lead.id)
            .outerjoin(failed, failed.c.lead_id == Lead.id)
            .where(
                Lead.status.in_(statuses),
                Lead.domain.is_not(None),
                Enrichment.status == EnrichmentStatusDB.OK,
                Lead.id.in_(
                    select(Contact.lead_id).where(
                        Contact.kind == "role",
                        ~exists().where(
                            Suppression.kind == "email",
                            func.lower(Suppression.value) == func.lower(Contact.email),
                        ),
                    )
                ),
                ~exists().where(
                    Suppression.kind == "domain",
                    func.lower(Suppression.value) == func.lower(Lead.domain),
                ),
            )
        )
        if lead_id is not None:
            stmt = stmt.where(Lead.id == lead_id)
        if source:
            stmt = stmt.where(Lead.source == source)
        if not regenerate:
            stmt = stmt.where(~exists().where(Message.lead_id == Lead.id))
        if not retry_failed:
            stmt = stmt.where(func.coalesce(failed.c.failed_attempts, 0) < max_attempts)
        return stmt, (func.coalesce(failed.c.failed_attempts, 0), Lead.id)

    async def eligibility_map(
        self,
        *,
        lead_ids: Sequence[int] | None = None,
        source: str | None = None,
        compose_max_attempts: int = 3,
        enrich_max_attempts: int = 3,
        retry_failed: bool = False,
    ) -> dict[int, Eligibility]:
        """Classify leads with :func:`evaluate`, in five queries regardless of size.

        ``leads --json``, ``compose --check`` and the GUI's stage strip all read
        this, so a lead can never be "draftable" in one place and not another.
        """
        stmt = select(Lead.id, Lead.status, Lead.domain).order_by(Lead.id)
        if source:
            stmt = stmt.where(Lead.source == source)
        if lead_ids is not None:
            wanted = list(lead_ids)
            if not wanted:
                return {}
            stmt = stmt.where(Lead.id.in_(wanted))
        lead_rows = [(row[0], row[1], row[2]) for row in await self.session.execute(stmt)]
        if not lead_rows:
            return {}

        role_contacts: dict[int, list[str]] = {}
        for contact_lead, email in await self.session.execute(
            select(Contact.lead_id, Contact.email).where(Contact.kind == "role")
        ):
            role_contacts.setdefault(contact_lead, []).append(email)

        enrichment_status = {
            lead_id: status
            for lead_id, status in await self.session.execute(
                select(Enrichment.lead_id, Enrichment.status)
            )
        }
        enrichment_attempts = {
            lead_id: int(attempts or 0)
            for lead_id, attempts in await self.session.execute(
                select(Enrichment.lead_id, Enrichment.attempts)
            )
        }
        with_message = set(
            (await self.session.execute(select(Message.lead_id).distinct())).scalars()
        )
        compose_failures = {
            int(entity_id): int(count)
            for entity_id, count in await self.session.execute(
                select(Event.entity_id, func.count())
                .where(Event.entity == "lead", Event.action == "compose_failed")
                .group_by(Event.entity_id)
            )
        }
        suppressions = await EnrichmentRepo(self.session).load_suppressions()

        result: dict[int, Eligibility] = {}
        for lead_id, status, domain in lead_rows:
            result[lead_id] = evaluate(
                _LeadFacts(status=str(status or ""), domain=domain),
                has_message=lead_id in with_message,
                role_contacts=role_contacts.get(lead_id, ()),
                suppressed_emails=suppressions.emails,
                suppressed_domains=suppressions.domains,
                enrichment_status=enrichment_status.get(lead_id),
                compose_attempts=compose_failures.get(lead_id, 0),
                compose_max_attempts=compose_max_attempts,
                enrich_attempts=enrichment_attempts.get(lead_id, 0),
                enrich_max_attempts=enrich_max_attempts,
                retry_failed=retry_failed,
            )
        return result

    async def funnel(
        self,
        *,
        compose_max_attempts: int = 3,
        enrich_max_attempts: int = 3,
    ) -> dict[str, int]:
        """Counts for the stage strip: how much work waits at each step."""
        eligibility = await self.eligibility_map(
            compose_max_attempts=compose_max_attempts,
            enrich_max_attempts=enrich_max_attempts,
        )
        total = len(eligibility)
        with_domain = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(Lead).where(Lead.domain.is_not(None))
                )
            ).scalar_one()
        )
        states: dict[str, int] = {}
        for item in eligibility.values():
            states[str(item.state)] = states.get(str(item.state), 0) + 1
        return {
            "collected": total,
            "with_domain": with_domain,
            "domainless": total - with_domain,
            "enrichable": states.get(LeadState.COLLECTED, 0)
            + states.get(LeadState.READY_TO_ENRICH, 0),
            "composable": states.get(LeadState.READY_TO_DRAFT, 0),
            "drafted": states.get(LeadState.DRAFTED, 0),
            "approved": states.get(LeadState.APPROVED, 0),
            "parked": states.get(LeadState.PARKED, 0),
            "done": states.get(LeadState.DONE, 0),
        }

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


class ComposeRepo:
    """Read and write helpers for Phase 3 drafts and their review thread."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest_message(self, lead_id: int) -> Message | None:
        stmt = (
            select(Message).where(Message.lead_id == lead_id).order_by(Message.id.desc()).limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_messages(
        self,
        *,
        limit: int = 20,
        lead_id: int | None = None,
        message_id: int | None = None,
    ) -> Sequence[tuple[Message, Lead, Contact | None]]:
        if message_id is not None:
            stmt = (
                select(Message, Lead, Contact)
                .join(Lead, Lead.id == Message.lead_id)
                .outerjoin(Contact, Contact.id == Message.contact_id)
                .where(Message.id == message_id)
            )
        else:
            latest = (
                select(Message.lead_id, func.max(Message.id).label("id"))
                .group_by(Message.lead_id)
                .subquery()
            )
            stmt = (
                select(Message, Lead, Contact)
                .join(latest, latest.c.id == Message.id)
                .join(Lead, Lead.id == Message.lead_id)
                .outerjoin(Contact, Contact.id == Message.contact_id)
                .order_by(Message.id.desc())
                .limit(limit)
            )
            if lead_id is not None:
                stmt = stmt.where(Message.lead_id == lead_id)
        rows = await self.session.execute(stmt)
        return [(message, lead, contact) for message, lead, contact in rows]

    async def failed_attempts(self, lead_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Event)
            .where(
                Event.entity == "lead",
                Event.entity_id == lead_id,
                Event.action == "compose_failed",
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def save_draft(
        self,
        *,
        lead_id: int,
        contact_id: int,
        subject: str,
        body: str,
        model: str | None,
        prompt_version: str,
        hooks: dict[str, Any],
    ) -> Message:
        lead = await self.session.get(Lead, lead_id)
        if lead is None:
            raise ValueError(f"lead {lead_id} does not exist")
        if lead.status not in (LeadStatusDB.ENRICHED, LeadStatusDB.COMPOSED):
            raise ValueError(f"lead {lead_id} is not composable ({lead.status})")

        message = Message(
            lead_id=lead_id,
            contact_id=contact_id,
            subject=subject,
            body=body,
            model=model,
            prompt_version=prompt_version,
            hooks=hooks,
        )
        self.session.add(message)
        await self.session.flush()
        lead.status = LeadStatusDB.COMPOSED
        self.session.add(
            Event(
                entity="lead",
                entity_id=lead_id,
                action="composed",
                detail={
                    "message_id": message.id,
                    "contact_id": contact_id,
                    "model": model,
                    "prompt_version": prompt_version,
                    "context_hash": hooks.get("context_hash"),
                    "validation": hooks.get("validation"),
                    "footer_version": hooks.get("footer_version"),
                },
            )
        )
        await self.session.flush()
        return message

    async def record_skip(self, lead_id: int, reason: str) -> None:
        self.session.add(
            Event(
                entity="lead",
                entity_id=lead_id,
                action="compose_skipped",
                detail={"reason": reason},
            )
        )
        await self.session.flush()

    async def record_failure(
        self,
        lead_id: int,
        *,
        reason: str,
        errors: list[str] | None = None,
        model: str | None = None,
        prompt_version: str = "",
        context_hash: str | None = None,
    ) -> None:
        failed_attempts = await self.failed_attempts(lead_id)
        self.session.add(
            Event(
                entity="lead",
                entity_id=lead_id,
                action="compose_failed",
                detail={
                    "reason": reason,
                    "errors": errors or [],
                    "model": model,
                    "prompt_version": prompt_version,
                    "context_hash": context_hash,
                    "failed_attempts": failed_attempts + 1,
                },
            )
        )
        await self.session.flush()

    async def create_suppression(
        self,
        *,
        value: str,
        kind: str,
        reason: str | None = None,
    ) -> Suppression:
        value = value.strip().lower()
        if not value:
            raise ValueError("suppression value is empty")
        stmt = select(Suppression).where(Suppression.value == value)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing
        row = Suppression(value=value, kind=kind, reason=reason)
        self.session.add(row)
        await self.session.flush()
        return row


def _valid_email(value: str | None) -> bool:
    if not value:
        return False
    if not value.isascii() or any(ord(char) < 32 for char in value):
        return False
    if any(char in value for char in " \t\r\n"):
        return False
    local, separator, domain = value.partition("@")
    return bool(separator and local and domain and "@" not in domain)


class MailRepo:
    """Approval ledger, send reservations and the manual ops loop."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -------------------------------------------------------------- approval
    async def latest_message(self, lead_id: int) -> Message | None:
        stmt = (
            select(Message).where(Message.lead_id == lead_id).order_by(Message.id.desc()).limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def approval_event(self, message_id: int) -> Event | None:
        stmt = (
            select(Event)
            .where(
                Event.entity == "message",
                Event.entity_id == message_id,
                Event.action.in_(("approved", "unapproved")),
            )
            .order_by(Event.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def is_approved(self, message_id: int) -> bool:
        event = await self.approval_event(message_id)
        return event is not None and event.action == "approved"

    async def approve(
        self,
        lead_id: int,
        *,
        message_id: int | None = None,
        expected_footer_version: str,
        expected_prompt_version: str | None = None,
        bulk: bool = False,
    ) -> tuple[Message, bool]:
        """Approve the latest draft; returns ``(message, already_approved)``."""
        lead = await self.session.get(Lead, lead_id)
        if lead is None:
            raise ApprovalRefused("lead_missing")
        message = await self.latest_message(lead_id)
        if message is None:
            raise ApprovalRefused("no_message")
        if message_id is not None and message.id != message_id:
            raise ApprovalRefused("not_latest", {"latest": message.id, "requested": message_id})

        if message.contact_id is None:
            raise ApprovalRefused("contact_missing")
        contact = await self.session.get(Contact, message.contact_id)
        if contact is None or contact.lead_id != lead.id:
            raise ApprovalRefused("contact_missing")

        suppressions = await EnrichmentRepo(self.session).load_suppressions()
        email_domain = contact.email.rsplit("@", 1)[-1]
        if (
            suppressions.blocks_email(contact.email)
            or suppressions.blocks_domain(lead.domain)
            or suppressions.blocks_domain(email_domain)
        ):
            raise ApprovalRefused("suppressed")

        footer_version = (message.hooks or {}).get("footer_version")
        if footer_version != expected_footer_version:
            raise ApprovalRefused("stale_footer", {"footer_version": footer_version})

        already = lead.status == LeadStatusDB.QUEUED and await self.is_approved(message.id)
        if not already and lead.status != LeadStatusDB.COMPOSED:
            raise ApprovalRefused("not_composed", {"status": lead.status})

        prompt_warning = bool(
            expected_prompt_version and message.prompt_version != expected_prompt_version
        )
        lead.status = LeadStatusDB.QUEUED
        self.session.add(
            Event(
                entity="message",
                entity_id=message.id,
                action="approved",
                detail={
                    "lead_id": lead.id,
                    "contact_id": contact.id,
                    "to_email": contact.email.strip().lower(),
                    "footer_version": footer_version,
                    "prompt_version": message.prompt_version,
                    "prompt_version_warning": prompt_warning,
                    "bulk": bulk,
                },
            )
        )
        await self.session.flush()
        return message, already

    async def unapprove(self, lead_id: int, *, reason: str | None = None) -> None:
        lead = await self.session.get(Lead, lead_id)
        if lead is None:
            raise ApprovalRefused("lead_missing")
        if lead.status != LeadStatusDB.QUEUED:
            raise ApprovalRefused("not_queued", {"status": lead.status})
        message = await self.latest_message(lead_id)
        if message is None:
            raise ApprovalRefused("no_message")
        live = await self.session.execute(
            select(func.count())
            .select_from(Send)
            .where(Send.message_id == message.id, Send.status.in_(LIVE_STATUSES))
        )
        if live.scalar_one():
            raise ApprovalRefused("has_live_send")

        lead.status = LeadStatusDB.COMPOSED
        self.session.add(
            Event(
                entity="message",
                entity_id=message.id,
                action="unapproved",
                detail={"lead_id": lead.id, "message_id": message.id, "reason": reason},
            )
        )
        await self.session.flush()

    async def composed_lead_ids(self, *, limit: int | None = None) -> list[int]:
        stmt = (
            select(Lead.id)
            .where(
                Lead.status == LeadStatusDB.COMPOSED,
                exists().where(Message.lead_id == Lead.id),
            )
            .order_by(Lead.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    # ------------------------------------------------------------- selection
    async def select_queued(
        self,
        *,
        lead_id: int | None = None,
        source: str | None = None,
        limit: int | None = None,
    ) -> list[QueuedMessage]:
        latest = (
            select(Message.lead_id, func.max(Message.id).label("id"))
            .group_by(Message.lead_id)
            .subquery()
        )
        attempts = (
            select(Send.message_id.label("message_id"), func.count().label("attempts"))
            .where(Send.status.in_(ATTEMPT_STATUSES))
            .group_by(Send.message_id)
            .subquery()
        )
        failed = (
            select(
                Send.message_id.label("message_id"),
                func.max(Send.created_at).label("last_failed"),
            )
            .where(Send.status == SendStatus.FAILED)
            .group_by(Send.message_id)
            .subquery()
        )
        stmt = (
            select(
                Lead,
                Message,
                Contact,
                func.coalesce(attempts.c.attempts, 0),
                failed.c.last_failed,
            )
            .join(latest, latest.c.lead_id == Lead.id)
            .join(Message, Message.id == latest.c.id)
            .outerjoin(Contact, Contact.id == Message.contact_id)
            .outerjoin(attempts, attempts.c.message_id == Message.id)
            .outerjoin(failed, failed.c.message_id == Message.id)
            .where(Lead.status == LeadStatusDB.QUEUED)
            .order_by(func.coalesce(attempts.c.attempts, 0), Lead.id)
        )
        if lead_id is not None:
            stmt = stmt.where(Lead.id == lead_id)
        if source is not None:
            stmt = stmt.where(Lead.source == source)
        rows = list((await self.session.execute(stmt)).unique().all())

        message_ids = [message.id for _, message, _, _, _ in rows]
        approved: dict[int, bool] = {}
        if message_ids:
            events = await self.session.execute(
                select(Event)
                .where(
                    Event.entity == "message",
                    Event.entity_id.in_(message_ids),
                    Event.action.in_(("approved", "unapproved")),
                )
                .order_by(Event.id)
            )
            for event in events.scalars():
                approved[event.entity_id] = event.action == "approved"

        snapshots: list[QueuedMessage] = []
        for lead, message, contact, attempt_count, last_failed in rows:
            if not approved.get(message.id) or contact is None:
                continue
            snapshots.append(
                QueuedMessage(
                    lead_id=lead.id,
                    source=lead.source,
                    lead_domain=lead.domain,
                    message_id=message.id,
                    contact_id=message.contact_id,
                    to_email=contact.email.strip().lower(),
                    subject=message.subject,
                    body=message.body,
                    prompt_version=message.prompt_version,
                    footer_version=(message.hooks or {}).get("footer_version"),
                    attempts=int(attempt_count or 0),
                    last_failed_at=ensure_utc(last_failed),
                )
            )
        if limit is not None:
            snapshots = snapshots[:limit]
        return snapshots

    # --------------------------------------------------------------- recheck
    async def attempts_for(self, message_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Send)
            .where(Send.message_id == message_id, Send.status.in_(ATTEMPT_STATUSES))
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def failed_attempts_for(self, message_id: int) -> tuple[int, datetime | None]:
        stmt = select(func.count(), func.max(Send.created_at)).where(
            Send.message_id == message_id, Send.status == SendStatus.FAILED
        )
        count, last = (await self.session.execute(stmt)).one()
        return int(count or 0), ensure_utc(last)

    async def _live_contacts(self) -> list[tuple[int, str, str | None]]:
        stmt = (
            select(Lead.id, Send.to_email, Lead.domain)
            .join(Message, Message.id == Send.message_id)
            .join(Lead, Lead.id == Message.lead_id)
            .where(Send.status.in_(LIVE_STATUSES))
        )
        return [
            (row[0], row[1].strip().lower(), row[2]) for row in await self.session.execute(stmt)
        ]

    async def recheck(
        self,
        snapshot: QueuedMessage,
        *,
        now: datetime,
        expected_footer_version: str,
        max_attempts: int,
        backoff_base: float = 300.0,
        backoff_cap: float = 3600.0,
        allowlist: frozenset[str] = frozenset(),
        window_open: bool = True,
        under_cap: bool = True,
        check_policy: bool = True,
        check_uniqueness: bool = True,
    ) -> RecheckResult:
        """Authoritative pre-send check list from Phase 4 §6."""
        now = ensure_utc(now) or utcnow()
        lead = await self.session.get(Lead, snapshot.lead_id)
        if lead is None or lead.status != LeadStatusDB.QUEUED:
            return RecheckResult(
                RecheckReason.NOT_QUEUED, detail={"status": getattr(lead, "status", None)}
            )

        message = await self.latest_message(lead.id)
        if message is None or message.id != snapshot.message_id:
            return RecheckResult(
                RecheckReason.STALE_APPROVAL,
                detail={"latest": getattr(message, "id", None), "approved": snapshot.message_id},
            )
        approval = await self.approval_event(message.id)
        if approval is None or approval.action != "approved":
            return RecheckResult(RecheckReason.NOT_APPROVED)
        if message.contact_id is None:
            return RecheckResult(RecheckReason.CONTACT_MISSING)
        contact = await self.session.get(Contact, message.contact_id)
        if contact is None or contact.lead_id != lead.id:
            return RecheckResult(RecheckReason.CONTACT_MISSING)

        email = contact.email.strip().lower()
        approved_email = (approval.detail or {}).get("to_email")
        if approved_email and approved_email != email:
            return RecheckResult(RecheckReason.CONTACT_CHANGED)
        if not _valid_email(email):
            return RecheckResult(RecheckReason.EMAIL_INVALID)

        suppressions = await EnrichmentRepo(self.session).load_suppressions()
        email_domain = email.rsplit("@", 1)[-1]
        if (
            suppressions.blocks_email(email)
            or suppressions.blocks_domain(lead.domain)
            or suppressions.blocks_domain(email_domain)
        ):
            return RecheckResult(RecheckReason.SUPPRESSED)

        footer_version = (message.hooks or {}).get("footer_version")
        if footer_version != expected_footer_version:
            return RecheckResult(
                RecheckReason.STALE_FOOTER, detail={"footer_version": footer_version}
            )

        attempts = await self.attempts_for(message.id)
        if attempts >= max_attempts:
            return RecheckResult(RecheckReason.ATTEMPTS_EXHAUSTED, detail={"attempts": attempts})
        failed_count, last_failed = await self.failed_attempts_for(message.id)
        not_before = retry_not_before(last_failed, failed_count, base=backoff_base, cap=backoff_cap)
        if not_before is not None and now < not_before:
            return RecheckResult(
                RecheckReason.RETRY_NOT_BEFORE,
                defer=True,
                detail={"retry_not_before": not_before.isoformat()},
            )

        existing = await self.session.execute(
            select(func.count())
            .select_from(Send)
            .where(Send.message_id == message.id, Send.status.in_(LIVE_STATUSES))
        )
        if existing.scalar_one():
            return RecheckResult(RecheckReason.EXISTING_ATTEMPT)

        if check_policy:
            if not window_open:
                return RecheckResult(RecheckReason.OUTSIDE_WINDOW, stop=True)
            if not under_cap:
                return RecheckResult(RecheckReason.DAILY_CAP, stop=True)
            if allowlist and not _is_allowlisted(email, lead.domain, allowlist):
                return RecheckResult(RecheckReason.NOT_ALLOWLISTED)

        if check_uniqueness:
            contacts = await self._live_contacts()
            domain = registrable_domain(lead.domain or email_domain)
            if domain and not is_freemail(domain):
                for other_lead, other_email, other_domain in contacts:
                    if other_lead == lead.id:
                        continue
                    other_registrable = registrable_domain(
                        other_domain or other_email.rsplit("@", 1)[-1]
                    )
                    if other_registrable == domain:
                        return RecheckResult(
                            RecheckReason.DOMAIN_ALREADY_CONTACTED, detail={"domain": domain}
                        )
            for _, other_email, _ in contacts:
                if other_email == email:
                    return RecheckResult(RecheckReason.RECIPIENT_ALREADY_CONTACTED)
        return RecheckResult()

    async def preview(
        self,
        snapshot: QueuedMessage,
        *,
        now: datetime,
        expected_footer_version: str,
        max_attempts: int,
        backoff_base: float = 300.0,
        backoff_cap: float = 3600.0,
    ) -> RecheckResult:
        """Message-integrity checks for a dry run (no policy/uniqueness gates)."""
        return await self.recheck(
            snapshot,
            now=now,
            expected_footer_version=expected_footer_version,
            max_attempts=max_attempts,
            backoff_base=backoff_base,
            backoff_cap=backoff_cap,
            check_policy=False,
            check_uniqueness=False,
        )

    async def reserve(
        self,
        snapshot: QueuedMessage,
        *,
        smtp_message_id: str,
        now: datetime,
        expected_footer_version: str,
        max_attempts: int,
        backoff_base: float = 300.0,
        backoff_cap: float = 3600.0,
        allowlist: frozenset[str] = frozenset(),
        window_open: bool = True,
        under_cap: bool = True,
    ) -> Reservation | RecheckResult:
        result = await self.recheck(
            snapshot,
            now=now,
            expected_footer_version=expected_footer_version,
            max_attempts=max_attempts,
            backoff_base=backoff_base,
            backoff_cap=backoff_cap,
            allowlist=allowlist,
            window_open=window_open,
            under_cap=under_cap,
        )
        if not result.ok:
            return result
        send = Send(
            message_id=snapshot.message_id,
            to_email=snapshot.to_email,
            mode="live",
            smtp_message_id=smtp_message_id,
            status=SendStatus.PENDING,
            created_at=ensure_utc(now) or utcnow(),
        )
        self.session.add(send)
        try:
            await self.session.flush()
        except IntegrityError:
            # The partial unique index (``uq_sends_message_live``) lost a race
            # with another process: that message already has a live attempt.
            # Only reads happened in this transaction, so rolling back is safe.
            await self.session.rollback()
            return RecheckResult(
                RecheckReason.EXISTING_ATTEMPT,
                detail={"message_id": snapshot.message_id, "guard": "db_unique_index"},
            )
        self.session.add(
            Event(
                entity="send",
                entity_id=send.id,
                action="send_attempt",
                detail={
                    "lead_id": snapshot.lead_id,
                    "message_id": snapshot.message_id,
                    "to_email": send.to_email,
                    "smtp_message_id": smtp_message_id,
                },
            )
        )
        await self.session.flush()
        return Reservation(
            send_id=send.id,
            message_id=snapshot.message_id,
            to_email=send.to_email,
            smtp_message_id=smtp_message_id,
        )

    # ------------------------------------------------------------- finalize
    async def finalize(
        self,
        send_id: int,
        *,
        status: str,
        stage: str = "",
        code: int | None = None,
        response: str | None = None,
        detail: str | None = None,
        now: datetime | None = None,
        allow_domain_suppression: bool = True,
    ) -> Send | None:
        send = await self.session.get(Send, send_id)
        if send is None:
            return None
        message = await self.session.get(Message, send.message_id)
        lead = await self.session.get(Lead, message.lead_id) if message else None
        stamp = ensure_utc(now) or utcnow()

        send.status = status
        if status == SendStatus.SENT:
            send.sent_at = stamp
            send.error = None
            if lead is not None and lead.status not in TERMINAL_LEAD_STATUSES:
                lead.status = LeadStatusDB.SENT
        else:
            send.sent_at = None
            parts = [stage or "unknown", str(code or ""), response or detail or ""]
            send.error = " ".join(part for part in parts if part).strip()[:500] or None

        if status == SendStatus.BOUNCED and lead is not None and message is not None:
            # A bounce is stronger than ``sent`` but never erases a reply or an
            # opt-out. ``allow_domain_suppression`` is False for inbound DSNs:
            # a stored DSN can be forwarded or list-mangled, so it may only
            # suppress the address it names.
            advance_lead_status(lead, LeadStatusDB.BOUNCED)
            enhanced = _enhanced_from(stage, code, response)
            await self._suppress(
                send.to_email,
                "email",
                f"bounce:{enhanced or code or 'unknown'}",
                now=stamp,
            )
            if allow_domain_suppression and enhanced == "5.1.2" and lead.domain:
                await self._suppress(
                    lead.domain,
                    "domain",
                    "bounce:5.1.2",
                    now=stamp,
                )

        self.session.add(
            Event(
                entity="send",
                entity_id=send.id,
                action=status,
                detail={
                    "lead_id": message.lead_id if message else None,
                    "message_id": send.message_id,
                    "to_email": send.to_email,
                    "stage": stage,
                    "code": code,
                    "response": (response or detail or "")[:500] or None,
                },
            )
        )
        await self.session.flush()
        return send

    async def recover_pending(self, *, now: datetime | None = None) -> int:
        """At live startup every pre-existing pending row becomes uncertain."""
        rows = list(
            (
                await self.session.execute(select(Send).where(Send.status == SendStatus.PENDING))
            ).scalars()
        )
        for send in rows:
            send.status = SendStatus.UNCERTAIN
            send.sent_at = None
            send.error = "recovered_after_crash"
            self.session.add(
                Event(
                    entity="send",
                    entity_id=send.id,
                    action="uncertain_recovered",
                    detail={"message_id": send.message_id, "to_email": send.to_email},
                )
            )
        await self.session.flush()
        return len(rows)

    # ---------------------------------------------------------------- policy
    async def count_sent_between(self, start: datetime, end: datetime) -> int:
        stamp = func.coalesce(Send.sent_at, Send.created_at)
        stmt = (
            select(func.count())
            .select_from(Send)
            .where(
                Send.status.in_((SendStatus.SENT, SendStatus.UNCERTAIN)),
                stamp >= ensure_utc(start),
                stamp < ensure_utc(end),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_uncertain(self) -> int:
        stmt = select(func.count()).select_from(Send).where(Send.status == SendStatus.UNCERTAIN)
        return int((await self.session.execute(stmt)).scalar_one())

    # ------------------------------------------------------------ manual ops
    async def _suppress(
        self,
        value: str,
        kind: str,
        reason: str | None,
        *,
        now: datetime | None = None,
    ) -> Suppression:
        value = value.strip().lower()
        if not value:
            raise ValueError("suppression value is empty")
        # ``uq_suppressions_value`` is on ``value`` alone: looking up
        # ``(value, kind)`` would miss a row and then fail the insert.
        existing = (
            await self.session.execute(select(Suppression).where(Suppression.value == value))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = Suppression(
            value=value,
            kind=kind,
            reason=reason,
            created_at=ensure_utc(now) or utcnow(),
        )
        self.session.add(row)
        await self.session.flush()
        self.session.add(
            Event(
                entity="suppression",
                entity_id=row.id,
                action="suppressed",
                detail={"value": value, "kind": kind, "reason": reason},
            )
        )
        await self.session.flush()
        return row

    async def destination_for_lead(self, lead_id: int) -> str | None:
        send = (
            await self.session.execute(
                select(Send.to_email)
                .join(Message, Message.id == Send.message_id)
                .where(Message.lead_id == lead_id)
                .order_by(Send.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if send:
            return send.strip().lower()
        message = await self.latest_message(lead_id)
        if message is not None and message.contact_id is not None:
            contact = await self.session.get(Contact, message.contact_id)
            if contact is not None:
                return contact.email.strip().lower()
        return None

    async def mark_lead(
        self,
        lead_id: int,
        status: str,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Lead:
        allowed = {"replied", "bounced", "sent", "suppressed"}
        if status not in allowed:
            raise ValueError(f"unsupported lead status: {status}")
        lead = await self.session.get(Lead, lead_id)
        if lead is None:
            raise LookupError(f"lead {lead_id} does not exist")

        if status == LeadStatusDB.SENT:
            live = await self.session.execute(
                select(func.count())
                .select_from(Send)
                .join(Message, Message.id == Send.message_id)
                .where(
                    Message.lead_id == lead_id,
                    Send.status.in_((SendStatus.SENT, SendStatus.UNCERTAIN)),
                )
            )
            if not live.scalar_one():
                raise ApprovalRefused("no_sent_or_uncertain_send")

        advance_lead_status(lead, status)
        stamp = ensure_utc(now) or utcnow()
        if status in (LeadStatusDB.BOUNCED, LeadStatusDB.SUPPRESSED):
            destination = await self.destination_for_lead(lead_id)
            if destination:
                await self._suppress(destination, "email", reason or f"marked:{status}", now=stamp)
        self.session.add(
            Event(
                entity="lead",
                entity_id=lead_id,
                action="marked",
                detail={"status": status, "reason": reason},
            )
        )
        await self.session.flush()
        return lead

    async def mark_send(
        self,
        send_id: int,
        status: str,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Send:
        if status not in (SendStatus.SENT, SendStatus.FAILED):
            raise ValueError(f"unsupported send status: {status}")
        send = await self.session.get(Send, send_id)
        if send is None:
            raise LookupError(f"send {send_id} does not exist")
        if send.status not in (SendStatus.PENDING, SendStatus.UNCERTAIN):
            raise ApprovalRefused("send_not_pending_or_uncertain", {"status": send.status})
        message = await self.session.get(Message, send.message_id)
        lead = await self.session.get(Lead, message.lead_id) if message else None
        stamp = ensure_utc(now) or utcnow()

        send.status = status
        if status == SendStatus.SENT:
            # ``created_at`` is the reservation time; documented as approximate.
            send.sent_at = ensure_utc(send.created_at) or stamp
            send.error = None
            if lead is not None and lead.status not in TERMINAL_LEAD_STATUSES:
                lead.status = LeadStatusDB.SENT
        else:
            send.sent_at = None
            send.error = reason or "marked_failed"
        self.session.add(
            Event(
                entity="send",
                entity_id=send.id,
                action="send_marked",
                detail={
                    "lead_id": message.lead_id if message else None,
                    "message_id": send.message_id,
                    "status": status,
                    "reason": reason,
                },
            )
        )
        await self.session.flush()
        return send

    async def unsuppress(
        self,
        *,
        value: str,
        kind: str,
        reason: str | None = None,
    ) -> Suppression:
        normalized = value.strip().lower()
        row = (
            await self.session.execute(select(Suppression).where(Suppression.value == normalized))
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(f"no {kind} suppression for {normalized}")
        if row.kind != kind:
            raise LookupError(
                f"{normalized} is suppressed as {row.kind}, not {kind}; pass --{row.kind}"
            )
        prior = {"value": row.value, "kind": row.kind, "reason": row.reason}
        row_id = row.id
        await self.session.delete(row)
        self.session.add(
            Event(
                entity="suppression",
                entity_id=row_id,
                action="unsuppressed",
                detail={**prior, "note": reason},
            )
        )
        await self.session.flush()
        return Suppression(value=prior["value"], kind=prior["kind"], reason=prior["reason"])

    async def send_status_counts(self) -> dict[str, int]:
        stmt = select(Send.status, func.count()).group_by(Send.status)
        return {str(status): int(count) for status, count in await self.session.execute(stmt)}

    async def latest_run_per_stage(self) -> dict[str, Event]:
        """Most recent ``run`` ledger row per stage (for ``status``/``doctor``)."""
        rows = list(
            (
                await self.session.execute(
                    select(Event).where(Event.entity == "run").order_by(Event.id.desc())
                )
            ).scalars()
        )
        latest: dict[str, Event] = {}
        for event in rows:
            stage = (event.detail or {}).get("stage")
            if stage and stage not in latest:
                latest[str(stage)] = event
        return latest

    async def list_sends(
        self,
        *,
        statuses: Sequence[str] | None = None,
        lead_id: int | None = None,
        limit: int = 50,
    ) -> list[tuple[Send, Message, Lead]]:
        stmt = (
            select(Send, Message, Lead)
            .join(Message, Message.id == Send.message_id)
            .join(Lead, Lead.id == Message.lead_id)
            .order_by(Send.id.desc())
            .limit(limit)
        )
        if statuses:
            stmt = stmt.where(Send.status.in_(statuses))
        if lead_id is not None:
            stmt = stmt.where(Lead.id == lead_id)
        rows = await self.session.execute(stmt)
        return [(send, message, lead) for send, message, lead in rows]


def _is_allowlisted(email: str, domain: str | None, allowlist: frozenset[str]) -> bool:
    # Kept local to avoid an import cycle with ``mailer.policy`` at module load.
    from forgesmith.mailer.policy import is_allowlisted

    return is_allowlisted(email, domain, allowlist)


def _enhanced_from(stage: str, code: int | None, response: str | None) -> str | None:
    from forgesmith.mailer.transport import enhanced_status

    return enhanced_status(response)


#: How many recent sends are scanned when an exact Message-ID lookup misses.
_ATTRIBUTION_SCAN = 500


class InboxRepo(MailRepo):
    """Phase 4.5 writes: one ledger row and at most one state transition.

    Inherits :class:`MailRepo` so ``finalize`` and ``_suppress`` behave exactly
    like the manual ``mark`` path. ``mark_lead`` is deliberately not reused: it
    suppresses the *latest send's* address, whereas a DSN must suppress the
    address the DSN itself names.
    """

    # ------------------------------------------------------------- mailbox
    async def get_mailbox(self, mailbox: str) -> MailboxState | None:
        return (
            await self.session.execute(select(MailboxState).where(MailboxState.mailbox == mailbox))
        ).scalar_one_or_none()

    async def prepare_mailbox(
        self,
        mailbox: str,
        uid_validity: int,
        *,
        now: datetime | None = None,
    ) -> tuple[MailboxState, bool]:
        """Return the cursor row, resetting it when UIDVALIDITY changed."""
        stamp = ensure_utc(now) or utcnow()
        state = (
            await self.session.execute(select(MailboxState).where(MailboxState.mailbox == mailbox))
        ).scalar_one_or_none()
        reset = False
        if state is None:
            state = MailboxState(
                mailbox=mailbox,
                uid_validity=uid_validity,
                last_uid=0,
                last_sync_at=stamp,
            )
            self.session.add(state)
            await self.session.flush()
        elif state.uid_validity != uid_validity:
            previous = state.uid_validity
            state.uid_validity = uid_validity
            state.last_uid = 0
            state.last_sync_at = stamp
            reset = True
            self.session.add(
                Event(
                    entity="mailbox",
                    entity_id=state.id,
                    action="uidvalidity_changed",
                    detail={"from": previous, "to": uid_validity, "source": "imap"},
                )
            )
        else:
            state.last_sync_at = stamp
        await self.session.flush()
        return state, reset

    async def advance_cursor(
        self,
        mailbox: str,
        uid: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Move the cursor forward only (a replayed UID never rewinds it)."""
        stamp = ensure_utc(now) or utcnow()
        result = await self.session.execute(
            update(MailboxState)
            .where(MailboxState.mailbox == mailbox, MailboxState.last_uid < uid)
            .values(last_uid=uid, last_sync_at=stamp)
        )
        return result.rowcount == 1

    # ---------------------------------------------------------- attribution
    async def _send_by_message_id(self, values: Sequence[str]) -> tuple[Send | None, bool]:
        wanted = {
            canonical for value in values if (canonical := canonical_message_id(value)) is not None
        }
        if not wanted:
            return None, False
        candidates: list[str] = []
        for token in wanted:
            candidates.extend((f"<{token}>", token))
        rows = list(
            (
                await self.session.execute(select(Send).where(Send.smtp_message_id.in_(candidates)))
            ).scalars()
        )
        if len(rows) == 1:
            return rows[0], False
        if len(rows) > 1:
            return None, True
        # Whitespace/case drift: compare canonically over the recent window.
        recent = list(
            (
                await self.session.execute(
                    select(Send).order_by(Send.id.desc()).limit(_ATTRIBUTION_SCAN)
                )
            ).scalars()
        )
        matches = [send for send in recent if canonical_message_id(send.smtp_message_id) in wanted]
        if len(matches) == 1:
            return matches[0], False
        return None, len(matches) > 1

    async def _send_by_recipient(self, address: str | None) -> Send | None:
        if not address:
            return None
        return (
            await self.session.execute(
                select(Send)
                .where(
                    func.lower(Send.to_email) == address.strip().lower(),
                    Send.status != SendStatus.ABORTED,
                )
                .order_by(Send.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _lead_for_send(self, send: Send) -> int | None:
        message = await self.session.get(Message, send.message_id)
        return message.lead_id if message is not None else None

    async def _latest_send_for_lead(self, lead_id: int) -> Send | None:
        return (
            await self.session.execute(
                select(Send)
                .join(Message, Message.id == Send.message_id)
                .where(Message.lead_id == lead_id)
                .order_by(Send.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _attribute_dsn(
        self, parsed: ParsedInbound
    ) -> tuple[int | None, int | None, dict[str, object]]:
        ids = [parsed.reported_message_id] if parsed.reported_message_id else []
        by_id, ambiguous = await self._send_by_message_id(ids)
        if ambiguous:
            return None, None, {"method": "ambiguous_message_id"}
        by_recipient = await self._send_by_recipient(parsed.final_recipient)
        if by_id is not None and by_recipient is not None and by_id.id != by_recipient.id:
            return (
                None,
                None,
                {
                    "method": "conflict",
                    "by_message_id": by_id.id,
                    "by_recipient": by_recipient.id,
                },
            )
        send = by_id or by_recipient
        if send is None:
            return None, None, {"method": "none"}
        return (
            await self._lead_for_send(send),
            send.id,
            {"method": "message_id" if by_id is not None else "recipient"},
        )

    async def _attribute_reply(
        self, parsed: ParsedInbound
    ) -> tuple[int | None, int | None, dict[str, object]]:
        send, ambiguous = await self._send_by_message_id(parsed.referenced_ids)
        if ambiguous:
            return None, None, {"method": "ambiguous_reference"}
        if send is not None:
            return await self._lead_for_send(send), send.id, {"method": "thread"}

        if parsed.from_addr:
            contacts = list(
                (
                    await self.session.execute(
                        select(Contact)
                        .where(func.lower(Contact.email) == parsed.from_addr)
                        .limit(2)
                    )
                ).scalars()
            )
            if len(contacts) == 1:
                lead_id = contacts[0].lead_id
                latest = await self._latest_send_for_lead(lead_id)
                return lead_id, latest.id if latest else None, {"method": "contact"}

        domain = registrable_domain(parsed.from_addr) if parsed.from_addr else None
        if domain:
            leads = list(
                (
                    await self.session.execute(select(Lead).where(Lead.domain == domain).limit(2))
                ).scalars()
            )
            if len(leads) == 1:
                latest = await self._latest_send_for_lead(leads[0].id)
                return leads[0].id, latest.id if latest else None, {"method": "domain"}

        return None, None, {"method": "none"}

    async def attribute(
        self, parsed: ParsedInbound
    ) -> tuple[int | None, int | None, dict[str, object]]:
        if parsed.kind == InboundKind.DSN:
            return await self._attribute_dsn(parsed)
        return await self._attribute_reply(parsed)

    # ------------------------------------------------------------- ledger
    @staticmethod
    def initial_status(
        parsed: ParsedInbound,
        *,
        send_id: int | None,
        lead_id: int | None,
    ) -> str:
        if parsed.kind in {
            InboundKind.ARF,
            InboundKind.MDN,
            InboundKind.AUTO,
            InboundKind.OTHER,
        }:
            return InboundStatusDB.IGNORED
        if parsed.kind == InboundKind.DSN:
            if parsed.is_hard_bounce:
                return InboundStatusDB.NEW if send_id is not None else InboundStatusDB.NEEDS_REVIEW
            return InboundStatusDB.IGNORED
        if parsed.kind == InboundKind.OPTOUT:
            # Never automatic: an opt-out is a legal decision.
            return InboundStatusDB.NEEDS_REVIEW
        if parsed.kind == InboundKind.REPLY:
            return InboundStatusDB.NEW if lead_id is not None else InboundStatusDB.NEEDS_REVIEW
        return InboundStatusDB.NEEDS_REVIEW

    @staticmethod
    def should_auto_apply(parsed: ParsedInbound, row: InboundMessage) -> bool:
        if row.status != InboundStatusDB.NEW:
            return False
        if parsed.kind == InboundKind.DSN:
            return parsed.is_hard_bounce and row.send_id is not None
        if parsed.kind == InboundKind.REPLY:
            return row.lead_id is not None
        return False

    async def record_inbound(
        self,
        parsed: ParsedInbound,
        raw: RawMessage,
        *,
        now: datetime | None = None,
    ) -> InboundMessage | None:
        """Insert the ledger row. Returns ``None`` when already ingested."""
        existing = (
            await self.session.execute(
                select(InboundMessage.id).where(
                    InboundMessage.mailbox == raw.mailbox,
                    InboundMessage.dedupe_hash == parsed.dedupe_hash,
                )
            )
        ).first()
        if existing is not None:
            return None

        lead_id, send_id, attribution = await self.attribute(parsed)
        status = self.initial_status(parsed, send_id=send_id, lead_id=lead_id)
        stamp = ensure_utc(now) or utcnow()
        detail = dict(parsed.detail)
        detail["attribution"] = attribution
        row = InboundMessage(
            mailbox=raw.mailbox,
            uid_validity=raw.uid_validity,
            imap_uid=raw.uid,
            dedupe_hash=parsed.dedupe_hash,
            rfc_message_id=parsed.rfc_message_id,
            kind=parsed.kind,
            status=status,
            from_addr=parsed.from_addr,
            to_addr=parsed.to_addr,
            final_recipient=parsed.final_recipient,
            subject=(parsed.subject or "")[:512] or None,
            lead_id=lead_id,
            send_id=send_id,
            reported_message_id=parsed.reported_message_id,
            enhanced_status=parsed.enhanced_status,
            smtp_code=parsed.smtp_code,
            detail=detail,
            received_at=parsed.received_at,
            seen_at=stamp,
        )
        self.session.add(row)
        await self.session.flush()
        self.session.add(
            Event(
                entity="inbound",
                entity_id=row.id,
                action="ingested",
                detail={
                    "kind": parsed.kind,
                    "status": status,
                    "lead_id": lead_id,
                    "send_id": send_id,
                    "source": "imap",
                },
            )
        )
        await self.session.flush()
        return row

    async def list_inbound(
        self,
        *,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[InboundMessage]:
        stmt = select(InboundMessage).order_by(InboundMessage.id.desc()).limit(limit)
        if statuses:
            stmt = stmt.where(InboundMessage.status.in_(statuses))
        return list((await self.session.execute(stmt)).scalars())

    async def inbound_status_counts(self) -> dict[str, int]:
        stmt = select(InboundMessage.status, func.count()).group_by(InboundMessage.status)
        return {str(status): int(count) for status, count in await self.session.execute(stmt)}

    async def get_inbound(self, inbound_id: int) -> InboundMessage | None:
        return await self.session.get(InboundMessage, inbound_id)

    # -------------------------------------------------------------- effects
    async def apply_inbound(
        self,
        row: InboundMessage,
        *,
        now: datetime | None = None,
        allow_optout: bool = False,
    ) -> bool:
        """Apply one ledger row exactly once (compare-and-swap on ``status``)."""
        is_optout = row.kind == InboundKind.OPTOUT
        if is_optout:
            if not allow_optout or row.status != InboundStatusDB.NEEDS_REVIEW:
                return False
            if row.lead_id is None or not row.from_addr:
                return False
            allowed = (InboundStatusDB.NEEDS_REVIEW,)
        else:
            if row.status != InboundStatusDB.NEW:
                return False
            allowed = (InboundStatusDB.NEW,)

        stamp = ensure_utc(now) or utcnow()
        claimed = await self.session.execute(
            update(InboundMessage)
            .where(
                InboundMessage.id == row.id,
                InboundMessage.status.in_(allowed),
            )
            .values(status=InboundStatusDB.APPLIED, applied_at=stamp)
        )
        if claimed.rowcount != 1:
            return False
        row.status = InboundStatusDB.APPLIED
        row.applied_at = stamp

        send = await self.session.get(Send, row.send_id) if row.send_id else None
        lead = await self.session.get(Lead, row.lead_id) if row.lead_id else None
        recipient = row.final_recipient or (send.to_email if send is not None else None)

        if row.kind == InboundKind.DSN and row.enhanced_status:
            if send is not None and send.status in {SendStatus.PENDING, SendStatus.UNCERTAIN}:
                # A DSN proves the transaction completed; it beats recover_pending
                # guessing "uncertain". The relay accepted it, so no domain
                # suppression: only the address the DSN names is burned.
                await self.finalize(
                    send.id,
                    status=SendStatus.BOUNCED,
                    stage="dsn",
                    code=row.smtp_code,
                    response=str(row.detail.get("diagnostic") or "") or None,
                    now=stamp,
                    allow_domain_suppression=False,
                )
            elif send is not None:
                if lead is not None:
                    advance_lead_status(lead, LeadStatusDB.BOUNCED)
                # ``Send.status`` is intentionally untouched: the relay accepted
                # the message, and flipping it would shrink the daily-cap count
                # and drop the row from LIVE_STATUSES guards.
                self.session.add(
                    Event(
                        entity="send",
                        entity_id=send.id,
                        action="dsn_bounce",
                        detail={
                            "inbound_message_id": row.id,
                            "enhanced_status": row.enhanced_status,
                            "smtp_code": row.smtp_code,
                            "recipient": recipient,
                            "source": "imap",
                        },
                    )
                )
            if recipient:
                await self._suppress(
                    recipient,
                    "email",
                    f"dsn:{row.enhanced_status or row.smtp_code or 'unknown'}",
                    now=stamp,
                )
        elif row.kind == InboundKind.REPLY and lead is not None:
            advance_lead_status(lead, LeadStatusDB.REPLIED)
            self.session.add(
                Event(
                    entity="lead",
                    entity_id=lead.id,
                    action="inbound_reply",
                    detail={
                        "inbound_message_id": row.id,
                        "send_id": row.send_id,
                        "source": "imap",
                    },
                )
            )
        elif is_optout and lead is not None:
            await self._suppress(
                row.from_addr or "",
                "email",
                f"optout:{row.detail.get('optout_phrase') or 'requested'}",
                now=stamp,
            )
            advance_lead_status(lead, LeadStatusDB.SUPPRESSED)
            self.session.add(
                Event(
                    entity="lead",
                    entity_id=lead.id,
                    action="inbound_optout",
                    detail={
                        "inbound_message_id": row.id,
                        "phrase": row.detail.get("optout_phrase"),
                        "source": "imap",
                    },
                )
            )

        await self.session.flush()
        return True
