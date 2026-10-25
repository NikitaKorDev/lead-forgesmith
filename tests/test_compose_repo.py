from __future__ import annotations

from sqlalchemy import select

from forgesmith.db.models import (
    Contact,
    Enrichment,
    EnrichmentStatusDB,
    Event,
    Lead,
    LeadStatusDB,
    Message,
    Suppression,
)
from forgesmith.db.repo import ComposeRepo, LeadRepo
from forgesmith.db.session import session_scope


async def _make_lead(
    source_id: str = "a.pl",
    *,
    status: LeadStatusDB = LeadStatusDB.ENRICHED,
    enrichment_status: EnrichmentStatusDB = EnrichmentStatusDB.OK,
    contacts: tuple[tuple[str, str, float], ...] = (("kontakt@a.pl", "role", 0.8),),
    domain: str | None = None,
) -> int:
    async with session_scope() as session:
        lead = Lead(
            source="shopify",
            source_id=source_id,
            name="Sklep",
            url=f"https://{source_id}/",
            domain=domain or source_id,
            status=status,
        )
        session.add(lead)
        await session.flush()
        session.add(
            Enrichment(
                lead_id=lead.id,
                status=enrichment_status,
                domain=lead.domain,
                description="Tworzymy meble z litego drewna.",
                context={"text_excerpt": "Prowadzimy pracownię meblową."},
            )
        )
        for email, kind, confidence in contacts:
            session.add(
                Contact(
                    lead_id=lead.id,
                    email=email,
                    kind=kind,
                    confidence=confidence,
                    source_url=f"https://{lead.domain}/kontakt",
                )
            )
        await session.flush()
        return lead.id


async def test_select_composable_requires_ok_enrichment_and_role_contact(db) -> None:
    await _make_lead("a.pl")
    await _make_lead("b.pl", contacts=(("osoba@b.pl", "personal", 0.9),))
    await _make_lead("c.pl", enrichment_status=EnrichmentStatusDB.NO_CONTACT)
    await _make_lead("d.pl", status=LeadStatusDB.NEW)

    async with session_scope() as session:
        leads = await LeadRepo(session).select_composable()

    assert [lead.domain for lead in leads] == ["a.pl"]


async def test_select_composable_skips_existing_drafts_unless_regenerating(db) -> None:
    lead_id = await _make_lead("a.pl")
    async with session_scope() as session:
        await ComposeRepo(session).save_draft(
            lead_id=lead_id,
            contact_id=1,
            subject="Temat",
            body="Treść",
            model="qwen",
            prompt_version="v1",
            hooks={},
        )

    async with session_scope() as session:
        assert await LeadRepo(session).select_composable() == []

    async with session_scope() as session:
        rows = await LeadRepo(session).select_composable(regenerate=True)
    assert [lead.id for lead in rows] == [lead_id]


async def test_select_composable_orders_by_failed_attempts_then_id(db) -> None:
    first = await _make_lead("a.pl")
    second = await _make_lead("b.pl")
    async with session_scope() as session:
        await ComposeRepo(session).record_failure(first, reason="parsing")

    async with session_scope() as session:
        rows = await LeadRepo(session).select_composable()

    assert [lead.id for lead in rows] == [second, first]


async def test_select_composable_respects_attempt_cap_and_retry_flag(db) -> None:
    lead_id = await _make_lead("a.pl")
    async with session_scope() as session:
        repo = ComposeRepo(session)
        for _ in range(3):
            await repo.record_failure(lead_id, reason="parsing")

    async with session_scope() as session:
        assert await LeadRepo(session).select_composable(max_attempts=3) == []

    async with session_scope() as session:
        rows = await LeadRepo(session).select_composable(
            max_attempts=3,
            retry_failed=True,
        )
    assert [lead.id for lead in rows] == [lead_id]


async def test_select_composable_excludes_suppressed_domains(db) -> None:
    await _make_lead("a.pl")
    await _make_lead("b.pl")
    async with session_scope() as session:
        await ComposeRepo(session).create_suppression(value="a.pl", kind="domain")

    async with session_scope() as session:
        rows = await LeadRepo(session).select_composable()

    assert [lead.domain for lead in rows] == ["b.pl"]


async def test_select_composable_excludes_leads_with_only_suppressed_roles(db) -> None:
    first = await _make_lead("a.pl")
    second = await _make_lead("b.pl", contacts=(("kontakt@b.pl", "role", 0.8),))
    async with session_scope() as session:
        await ComposeRepo(session).create_suppression(value="kontakt@a.pl", kind="email")

    async with session_scope() as session:
        rows = await LeadRepo(session).select_composable()

    assert [lead.id for lead in rows] == [second]
    assert first not in [lead.id for lead in rows]


async def test_latest_messages_returns_one_row_per_lead(db) -> None:
    first = await _make_lead("a.pl")
    second = await _make_lead("b.pl")

    async with session_scope() as session:
        repo = ComposeRepo(session)
        await repo.save_draft(
            lead_id=first,
            contact_id=1,
            subject="Pierwszy",
            body="Treść 1",
            model="qwen",
            prompt_version="v1",
            hooks={"context_hash": "a"},
        )
        await repo.save_draft(
            lead_id=first,
            contact_id=1,
            subject="Drugi",
            body="Treść 2",
            model="qwen",
            prompt_version="v1",
            hooks={"context_hash": "b"},
        )
        await repo.save_draft(
            lead_id=second,
            contact_id=2,
            subject="Trzeci",
            body="Treść 3",
            model="qwen",
            prompt_version="v1",
            hooks={"context_hash": "c"},
        )

    async with session_scope() as session:
        rows = await ComposeRepo(session).latest_messages(limit=10)

    assert [message.subject for message, _, _ in rows] == ["Trzeci", "Drugi"]
    assert {message.lead_id for message, _, _ in rows} == {first, second}


async def test_latest_messages_can_select_exact_id_and_lead(db) -> None:
    first = await _make_lead("a.pl")
    second = await _make_lead("b.pl")
    async with session_scope() as session:
        repo = ComposeRepo(session)
        one = await repo.save_draft(
            lead_id=first,
            contact_id=1,
            subject="Jeden",
            body="Treść",
            model="qwen",
            prompt_version="v1",
            hooks={},
        )
        await repo.save_draft(
            lead_id=second,
            contact_id=2,
            subject="Dwa",
            body="Treść",
            model="qwen",
            prompt_version="v1",
            hooks={},
        )

    async with session_scope() as session:
        by_id = await ComposeRepo(session).latest_messages(message_id=one.id)
        by_lead = await ComposeRepo(session).latest_messages(lead_id=second)

    assert [row[0].subject for row in by_id] == ["Jeden"]
    assert [row[0].subject for row in by_lead] == ["Dwa"]


async def test_save_draft_sets_composed_and_records_event(db) -> None:
    lead_id = await _make_lead("a.pl")
    async with session_scope() as session:
        message = await ComposeRepo(session).save_draft(
            lead_id=lead_id,
            contact_id=1,
            subject="Temat",
            body="Treść",
            model="qwen",
            prompt_version="v1",
            hooks={"context_hash": "abc"},
        )
        lead = await session.get(Lead, lead_id)
        assert lead is not None
        assert lead.status == LeadStatusDB.COMPOSED

    async with session_scope() as session:
        saved = await session.get(Message, message.id)
        event = (
            await session.execute(
                select(Event).where(Event.action == "composed", Event.entity_id == lead_id)
            )
        ).scalar_one()
    assert saved is not None
    assert event.detail["context_hash"] == "abc"


async def test_save_draft_rejects_non_composable_status(db) -> None:
    lead_id = await _make_lead("a.pl", status=LeadStatusDB.SENT)
    async with session_scope() as session:
        try:
            await ComposeRepo(session).save_draft(
                lead_id=lead_id,
                contact_id=1,
                subject="Temat",
                body="Treść",
                model="qwen",
                prompt_version="v1",
                hooks={},
            )
        except ValueError as exc:
            assert "not composable" in str(exc)
        else:
            raise AssertionError("save_draft should reject a non-composable status")


async def test_suppression_is_idempotent_and_normalized(db) -> None:
    async with session_scope() as session:
        repo = ComposeRepo(session)
        first = await repo.create_suppression(value="A@Example.COM", kind="email")
        second = await repo.create_suppression(value="a@example.com", kind="email")
        assert first.id == second.id

    async with session_scope() as session:
        rows = (await session.execute(select(Suppression))).scalars().all()
    assert [(row.value, row.kind) for row in rows] == [("a@example.com", "email")]


async def test_failed_attempts_and_skip_events_are_append_only(db) -> None:
    lead_id = await _make_lead("a.pl")
    async with session_scope() as session:
        repo = ComposeRepo(session)
        assert await repo.failed_attempts(lead_id) == 0
        await repo.record_failure(lead_id, reason="parsing")
        await repo.record_skip(lead_id, "insufficient_context")
        assert await repo.failed_attempts(lead_id) == 1

    async with session_scope() as session:
        actions = (
            (await session.execute(select(Event.action).where(Event.entity_id == lead_id)))
            .scalars()
            .all()
        )
    assert list(actions) == ["compose_failed", "compose_skipped"]
