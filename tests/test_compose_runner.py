from __future__ import annotations

import json

from sqlalchemy import func, select

from forgesmith.compose import compose_pending
from forgesmith.compose.models import LLMError
from forgesmith.compose.prompt import SenderProfile
from forgesmith.config import settings
from forgesmith.db.models import Contact, Enrichment, EnrichmentStatusDB, Event, Lead, Message
from forgesmith.db.repo import ComposeRepo
from forgesmith.db.session import session_scope

SENDER = SenderProfile(
    name="Jan Testowy",
    email="jan@example.com",
    company="Studio Testowe",
    offer="automatyzacja zbierania danych o produktach",
)
SENTENCE = "Prowadzimy sprzedaż mebli z litego drewna i tworzymy własne kolekcje."
VALID_CONTEXT = {
    "text_excerpt": SENTENCE,
    "description": "Tworzymy meble z litego drewna.",
    "headings": ["Nasza pracownia"],
    "products": [{"title": "Stół dębowy"}],
}


def _body(extra: str = "") -> str:
    return (
        "Dzień dobry,\n\n"
        + (SENTENCE + " ") * 7
        + extra
        + "Czy moglibyśmy porozmawiać o uproszczeniu zbierania danych o produktach? "
        "Zależy mi na krótkiej rozmowie."
    )


def _draft_json(
    *,
    subject: str = "Pomysł na uproszczenie oferty",
    body: str | None = None,
    evidence: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "subject": subject,
            "body": body if body is not None else _body(),
            "evidence": evidence if evidence is not None else ["sprzedaż mebli z litego drewna"],
        },
        ensure_ascii=False,
    )


class FakeLLM:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


async def _make_lead(
    source_id: str = "a.pl",
    *,
    context: dict | None = None,
    description: str = "Tworzymy meble z litego drewna.",
    contacts: tuple[tuple[str, str, float], ...] = (("biuro@a.pl", "role", 0.9),),
) -> int:
    async with session_scope() as session:
        lead = Lead(
            source="shopify",
            source_id=source_id,
            name="Sklep",
            url=f"https://{source_id}/",
            domain=source_id,
            status="enriched",
        )
        session.add(lead)
        await session.flush()
        session.add(
            Enrichment(
                lead_id=lead.id,
                status=EnrichmentStatusDB.OK,
                domain=source_id,
                storefront_platform="shopify",
                legal_name="Sklep sp. z o.o.",
                description=description,
                context=context if context is not None else VALID_CONTEXT,
            )
        )
        for email, kind, confidence in contacts:
            session.add(
                Contact(
                    lead_id=lead.id,
                    email=email,
                    kind=kind,
                    confidence=confidence,
                    source_url=f"https://{source_id}/kontakt",
                )
            )
        await session.flush()
        return lead.id


async def test_compose_happy_path_persists_latest_draft_and_event(db) -> None:
    lead_id = await _make_lead()
    fake = FakeLLM([_draft_json()])

    stats = await compose_pending(lead_id=lead_id, settings=settings, llm=fake, sender=SENDER)

    assert (stats.scanned, stats.drafted, stats.failed, stats.skipped) == (1, 1, 0, 0)
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        message = (await session.execute(select(Message))).scalar_one()
        event = (
            await session.execute(
                select(Event).where(Event.action == "composed", Event.entity_id == lead_id)
            )
        ).scalar_one()

    assert lead is not None and lead.status == "composed"
    assert message.body.count("Pozdrawiam,") == 1
    assert "jan@example.com" in message.body
    assert message.hooks["validation"]["errors"] == []
    assert message.hooks["evidence"] == ["sprzedaż mebli z litego drewna"]
    assert event.detail["context_hash"] == message.hooks["context_hash"]


async def test_second_run_is_idempotent_and_regenerate_adds_draft(db) -> None:
    lead_id = await _make_lead()
    first = FakeLLM([_draft_json(subject="Pierwszy")])
    await compose_pending(lead_id=lead_id, settings=settings, llm=first, sender=SENDER)

    second = FakeLLM([])
    stats = await compose_pending(lead_id=lead_id, settings=settings, llm=second, sender=SENDER)
    assert stats.scanned == 0
    assert second.calls == []

    third = FakeLLM([_draft_json(subject="Drugi")])
    stats = await compose_pending(
        lead_id=lead_id,
        settings=settings,
        llm=third,
        sender=SENDER,
        regenerate=True,
    )
    assert stats.drafted == 1

    async with session_scope() as session:
        count = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
        latest = await ComposeRepo(session).latest_message(lead_id)
    assert count == 2
    assert latest is not None and latest.subject == "Drugi"


async def test_invalid_first_answer_is_repaired_once(db) -> None:
    lead_id = await _make_lead()
    fake = FakeLLM(["not json", _draft_json(subject="Po naprawie")])

    stats = await compose_pending(lead_id=lead_id, settings=settings, llm=fake, sender=SENDER)

    assert stats.drafted == 1
    assert len(fake.calls) == 2
    async with session_scope() as session:
        message = (await session.execute(select(Message))).scalar_one()
    assert message.subject == "Po naprawie"


async def test_failed_regeneration_keeps_existing_composed_draft(db) -> None:
    lead_id = await _make_lead()
    await compose_pending(
        lead_id=lead_id,
        settings=settings,
        llm=FakeLLM([_draft_json(subject="Pierwszy")]),
        sender=SENDER,
    )

    fake = FakeLLM(["still not json", "still not json"])
    stats = await compose_pending(
        lead_id=lead_id,
        settings=settings,
        llm=fake,
        sender=SENDER,
        regenerate=True,
    )

    assert stats.failed == 1
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        count = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
        event = (
            await session.execute(
                select(Event).where(Event.action == "compose_failed", Event.entity_id == lead_id)
            )
        ).scalar_one()
    assert lead is not None and lead.status == "composed"
    assert count == 1
    assert event is not None


async def test_llm_error_records_failure_without_status_change(db) -> None:
    lead_id = await _make_lead()
    fake = FakeLLM([LLMError("ollama:ConnectError")])

    stats = await compose_pending(lead_id=lead_id, settings=settings, llm=fake, sender=SENDER)

    assert stats.failed == 1
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        event = (
            await session.execute(
                select(Event).where(Event.action == "compose_failed", Event.entity_id == lead_id)
            )
        ).scalar_one()
    assert lead is not None and lead.status == "enriched"
    assert event.detail["reason"] == "ollama:ConnectError"


async def test_suppressed_email_is_excluded_before_prompting(db) -> None:
    lead_id = await _make_lead()
    async with session_scope() as session:
        await ComposeRepo(session).create_suppression(value="biuro@a.pl", kind="email")
    fake = FakeLLM([])

    stats = await compose_pending(lead_id=lead_id, settings=settings, llm=fake, sender=SENDER)

    assert stats.scanned == 0
    assert stats.skipped == 0
    assert stats.failed == 0
    assert fake.calls == []


async def test_empty_context_is_skipped_before_prompting(db) -> None:
    lead_id = await _make_lead(context={}, description="")
    fake = FakeLLM([])

    stats = await compose_pending(lead_id=lead_id, settings=settings, llm=fake, sender=SENDER)

    assert stats.skipped == 1
    assert fake.calls == []
    async with session_scope() as session:
        event = (
            await session.execute(
                select(Event).where(Event.action == "compose_skipped", Event.entity_id == lead_id)
            )
        ).scalar_one()
    assert event.detail["reason"] == "insufficient_context"


async def test_only_personal_contact_is_not_selected(db) -> None:
    lead_id = await _make_lead(contacts=(("osoba@a.pl", "personal", 0.9),))
    fake = FakeLLM([])

    stats = await compose_pending(lead_id=lead_id, settings=settings, llm=fake, sender=SENDER)

    assert stats.scanned == 0
    assert fake.calls == []


async def test_model_must_receive_cleaned_context_not_raw_pages(db) -> None:
    lead_id = await _make_lead(
        context={
            **VALID_CONTEXT,
            "pages": ["https://a.pl/hidden"],
            "evidence": {"email": "sekret@a.pl"},
        }
    )
    fake = FakeLLM([_draft_json()])

    await compose_pending(lead_id=lead_id, settings=settings, llm=fake, sender=SENDER)

    _, user = fake.calls[0]
    assert "sekret@a.pl" not in user
    assert "https://a.pl/hidden" not in user
    assert "<untrusted_site_context>" in user
