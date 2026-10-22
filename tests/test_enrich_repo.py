from __future__ import annotations

from sqlalchemy import select

from forgesmith.db.models import (
    Contact,
    Enrichment,
    EnrichmentStatusDB,
    Event,
    Lead,
    LeadStatusDB,
    Suppression,
)
from forgesmith.db.repo import EnrichmentRepo, LeadRepo
from forgesmith.db.session import session_scope
from forgesmith.domain import LeadCandidate, LeadSource
from forgesmith.enrichment.models import EmailHit, EnrichmentResult


async def _make_lead(source_id: str = "a.pl", domain: str | None = "a.pl") -> int:
    candidate = LeadCandidate(
        source=LeadSource.SHOPIFY,
        source_id=source_id,
        name="Sklep",
        url=f"https://{source_id}/",
        domain=domain,
    )
    async with session_scope() as session:
        await LeadRepo(session).upsert_many([candidate])
        lead = await LeadRepo(session).by_source_id("shopify", source_id)
        assert lead is not None
        return lead.id


def _result(
    status: EnrichmentStatusDB = EnrichmentStatusDB.OK,
    *,
    domain: str | None = "a.pl",
    contacts: list[EmailHit] | None = None,
    reason: str | None = None,
) -> EnrichmentResult:
    return EnrichmentResult(
        status=status,
        domain=domain,
        domain_source="source",
        site_url=f"https://{domain}/" if domain else None,
        storefront_platform="woocommerce",
        context={"text_excerpt": "Sklep z drewnem"},
        evidence={"pages": []},
        contacts=contacts or [],
        engine="http",
        promote_domain=domain is not None,
        reason=reason,
    )


def _hit(email: str = "kontakt@a.pl", confidence: float = 1.0) -> EmailHit:
    local, domain = email.split("@", 1)
    return EmailHit(
        email=email,
        kind="role",
        confidence=confidence,
        source_url=f"https://{domain}/kontakt",
        local=local,
        domain=domain,
    )


async def test_db_init_creates_enrichments_table(db) -> None:
    async with session_scope() as session:
        rows = (await session.execute(select(Enrichment))).scalars().all()
    assert rows == []


async def test_claim_attempt_creates_row_and_increments(db) -> None:
    lead_id = await _make_lead()

    async with session_scope() as session:
        assert await EnrichmentRepo(session).claim_attempt(lead_id) == 1

    async with session_scope() as session:
        repo = EnrichmentRepo(session)
        row = await repo.get(lead_id)
        assert row is not None
        assert row.status is None
        assert row.attempts == 1
        assert row.last_attempt_at is not None
        assert await repo.claim_attempt(lead_id) == 2

    async with session_scope() as session:
        row = await EnrichmentRepo(session).get(lead_id)
        assert row is not None
        assert row.attempts == 2


async def test_finish_ok_promotes_domain_stores_contact_and_event(db) -> None:
    lead_id = await _make_lead(domain=None)

    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(lead_id)

    async with session_scope() as session:
        outcome = await EnrichmentRepo(session).finish(lead_id, _result(contacts=[_hit()]))

    assert outcome.lead_status == LeadStatusDB.ENRICHED
    assert outcome.stored_contacts == 1

    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        enrichment = await EnrichmentRepo(session).get(lead_id)
        contacts = (await session.execute(select(Contact))).scalars().all()
        events = (await session.execute(select(Event))).scalars().all()

    assert lead is not None and lead.status == LeadStatusDB.ENRICHED
    assert lead.domain == "a.pl"
    assert enrichment is not None
    assert enrichment.status == EnrichmentStatusDB.OK
    assert enrichment.last_success_at is not None
    assert [contact.email for contact in contacts] == ["kontakt@a.pl"]
    assert contacts[0].verified_at is None
    assert len(events) == 1
    assert events[0].action == "enriched"
    assert events[0].detail["emails"] == 1


async def test_finish_upserts_contacts_instead_of_duplicating(db) -> None:
    lead_id = await _make_lead()

    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(lead_id)
    async with session_scope() as session:
        await EnrichmentRepo(session).finish(lead_id, _result(contacts=[_hit()]))
    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(lead_id)
    async with session_scope() as session:
        await EnrichmentRepo(session).finish(
            lead_id,
            _result(
                contacts=[
                    EmailHit(
                        email="Kontakt@A.pl",
                        kind="role",
                        confidence=0.8,
                        source_url="https://a.pl/o-nas",
                        local="Kontakt",
                        domain="A.pl",
                    )
                ]
            ),
        )

    async with session_scope() as session:
        contacts = (await session.execute(select(Contact))).scalars().all()
    assert len(contacts) == 1
    assert contacts[0].confidence == 0.8
    assert contacts[0].source_url == "https://a.pl/o-nas"


async def test_force_never_downgrades_enriched_lead(db) -> None:
    lead_id = await _make_lead()

    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(lead_id)
    async with session_scope() as session:
        await EnrichmentRepo(session).finish(lead_id, _result(contacts=[_hit()]))

    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(lead_id)
    async with session_scope() as session:
        outcome = await EnrichmentRepo(session).finish(
            lead_id,
            _result(EnrichmentStatusDB.ERROR, contacts=[], reason="timeout"),
        )

    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        enrichment = await EnrichmentRepo(session).get(lead_id)
        contacts = (await session.execute(select(Contact))).scalars().all()
        events = (await session.execute(select(Event))).scalars().all()

    assert outcome.lead_status == LeadStatusDB.ENRICHED
    assert outcome.stored_contacts == 1
    assert lead is not None and lead.status == LeadStatusDB.ENRICHED
    assert enrichment is not None and enrichment.status == EnrichmentStatusDB.OK
    assert len(contacts) == 1
    assert events[-1].detail["retained"] is True


async def test_error_stays_new_until_max_attempts(db) -> None:
    lead_id = await _make_lead()
    error = _result(EnrichmentStatusDB.ERROR, domain=None, reason="timeout")

    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(lead_id)
    async with session_scope() as session:
        await EnrichmentRepo(session).finish(lead_id, error, max_attempts=3)
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
    assert lead is not None and lead.status == LeadStatusDB.NEW

    for attempt in (2, 3):
        async with session_scope() as session:
            assert await EnrichmentRepo(session).claim_attempt(lead_id) == attempt
        async with session_scope() as session:
            outcome = await EnrichmentRepo(session).finish(lead_id, error, max_attempts=3)

    assert outcome.lead_status == LeadStatusDB.SKIPPED


async def test_blocked_and_no_site_are_terminal_on_first_attempt(db) -> None:
    for index, status in enumerate((EnrichmentStatusDB.BLOCKED, EnrichmentStatusDB.NO_SITE)):
        lead_id = await _make_lead(source_id=f"site{index}.pl")
        async with session_scope() as session:
            await EnrichmentRepo(session).claim_attempt(lead_id)
        async with session_scope() as session:
            outcome = await EnrichmentRepo(session).finish(
                lead_id, _result(status, domain=f"site{index}.pl", reason=status.value)
            )
        assert outcome.lead_status == LeadStatusDB.SKIPPED


async def test_suppressed_email_and_domain_are_never_stored(db) -> None:
    lead_id = await _make_lead()
    async with session_scope() as session:
        session.add(Suppression(value="kontakt@a.pl", kind="email", reason="opt-out"))
    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(lead_id)
    async with session_scope() as session:
        outcome = await EnrichmentRepo(session).finish(
            lead_id, _result(contacts=[_hit("kontakt@a.pl"), _hit("biuro@a.pl")])
        )
    assert outcome.stored_contacts == 1

    other_id = await _make_lead(source_id="b.pl", domain="b.pl")
    async with session_scope() as session:
        session.add(Suppression(value="b.pl", kind="domain", reason="opt-out"))
    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(other_id)
    async with session_scope() as session:
        outcome = await EnrichmentRepo(session).finish(
            other_id, _result(domain="b.pl", contacts=[_hit("kontakt@b.pl")])
        )
    assert outcome.stored_contacts == 0

    async with session_scope() as session:
        stored = (await session.execute(select(Contact))).scalars().all()
    assert [contact.email for contact in stored] == ["biuro@a.pl"]


async def test_select_pending_orders_by_attempts_then_id(db) -> None:
    first = await _make_lead("a.pl", "a.pl")
    second = await _make_lead("b.pl", "b.pl")
    third = await _make_lead("c.pl", "c.pl")
    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(first)

    async with session_scope() as session:
        rows = await LeadRepo(session).select_pending(max_attempts=3)
    assert [row.id for row in rows] == [second, third, first]


async def test_select_pending_filters_and_force_whitelist(db) -> None:
    first = await _make_lead("a.pl", "a.pl")
    second = await _make_lead("b.pl", "b.pl")
    async with session_scope() as session:
        lead = await session.get(Lead, first)
        assert lead is not None
        lead.status = LeadStatusDB.SENT
        other = await session.get(Lead, second)
        assert other is not None
        other.status = LeadStatusDB.ENRICHED

    async with session_scope() as session:
        assert [row.id for row in await LeadRepo(session).select_pending()] == []
        forced = await LeadRepo(session).select_pending(force=True)
        assert [row.id for row in forced] == [second]


async def test_select_pending_ignores_leads_at_attempt_limit(db) -> None:
    lead_id = await _make_lead()
    async with session_scope() as session:
        await EnrichmentRepo(session).claim_attempt(lead_id)
        await EnrichmentRepo(session).claim_attempt(lead_id)
        await EnrichmentRepo(session).claim_attempt(lead_id)

    async with session_scope() as session:
        assert await LeadRepo(session).select_pending(max_attempts=3) == []
