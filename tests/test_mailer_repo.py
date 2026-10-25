from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from forgesmith.compose.prompt import FOOTER_VERSION, PROMPT_VERSION
from forgesmith.db.models import (
    Contact,
    Event,
    Lead,
    Message,
    Send,
    Suppression,
)
from forgesmith.db.repo import ComposeRepo, MailRepo
from forgesmith.db.session import session_scope
from forgesmith.mailer.models import (
    ApprovalRefused,
    QueuedMessage,
    SendStatus,
)

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


async def _snapshot(lead_id: int) -> QueuedMessage:
    async with session_scope() as session:
        repo = MailRepo(session)
        message = await repo.latest_message(lead_id)
        assert message is not None
        contact = await session.get(Contact, message.contact_id)
        lead = await session.get(Lead, lead_id)
        assert contact is not None and lead is not None
        return QueuedMessage(
            lead_id=lead.id,
            source=lead.source,
            lead_domain=lead.domain,
            message_id=message.id,
            contact_id=contact.id,
            to_email=contact.email.lower(),
            subject=message.subject,
            body=message.body,
            prompt_version=message.prompt_version,
            footer_version=(message.hooks or {}).get("footer_version"),
        )


async def test_approve_moves_composed_to_queued_and_audits(
    seed_queued,
) -> None:
    seeded = await seed_queued(lead_status="composed", approved=False)

    async with session_scope() as session:
        message, already = await MailRepo(session).approve(
            seeded.lead_id,
            expected_footer_version=FOOTER_VERSION,
            expected_prompt_version=PROMPT_VERSION,
        )

    assert message.id == seeded.message_id
    assert already is False
    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        events = list(
            (
                await session.execute(
                    select(Event).where(
                        Event.entity == "message",
                        Event.entity_id == seeded.message_id,
                        Event.action == "approved",
                    )
                )
            ).scalars()
        )
    assert lead is not None and lead.status == "queued"
    assert len(events) == 1
    assert events[0].detail["to_email"] == "biuro@a.pl"
    assert events[0].detail["footer_version"] == FOOTER_VERSION


async def test_approving_twice_is_an_idempotent_noop_with_event(seed_queued) -> None:
    seeded = await seed_queued()

    async with session_scope() as session:
        _, already = await MailRepo(session).approve(
            seeded.lead_id,
            expected_footer_version=FOOTER_VERSION,
        )

    assert already is True
    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        count = len(
            list(
                (
                    await session.execute(
                        select(Event).where(
                            Event.action == "approved",
                            Event.entity_id == seeded.message_id,
                        )
                    )
                ).scalars()
            )
        )
    assert lead is not None and lead.status == "queued"
    assert count == 2


async def test_approve_refuses_a_superseded_message(seed_queued) -> None:
    seeded = await seed_queued(lead_status="composed", approved=False)
    async with session_scope() as session:
        session.add(
            Message(
                lead_id=seeded.lead_id,
                contact_id=seeded.contact_id,
                subject="Nowszy",
                body="Dzień dobry,\n\nTreść.\n",
                hooks={"footer_version": FOOTER_VERSION},
                prompt_version=PROMPT_VERSION,
            )
        )

    async with session_scope() as session:
        with pytest.raises(ApprovalRefused, match="not_latest"):
            await MailRepo(session).approve(
                seeded.lead_id,
                message_id=seeded.message_id,
                expected_footer_version=FOOTER_VERSION,
            )


async def test_approve_refuses_suppressed_stale_footer_and_wrong_status(
    seed_queued,
) -> None:
    suppressed = await seed_queued(lead_status="composed", approved=False)
    async with session_scope() as session:
        await ComposeRepo(session).create_suppression(value="biuro@a.pl", kind="email")
    async with session_scope() as session:
        with pytest.raises(ApprovalRefused, match="suppressed"):
            await MailRepo(session).approve(
                suppressed.lead_id, expected_footer_version=FOOTER_VERSION
            )

    stale = await seed_queued(
        source_id="b.pl",
        email="biuro@b.pl",
        lead_status="composed",
        approved=False,
        footer_version="phase3-footer-old",
    )
    async with session_scope() as session:
        with pytest.raises(ApprovalRefused, match="stale_footer"):
            await MailRepo(session).approve(stale.lead_id, expected_footer_version=FOOTER_VERSION)

    wrong = await seed_queued(
        source_id="c.pl", email="biuro@c.pl", lead_status="enriched", approved=False
    )
    async with session_scope() as session:
        with pytest.raises(ApprovalRefused, match="not_composed"):
            await MailRepo(session).approve(wrong.lead_id, expected_footer_version=FOOTER_VERSION)


async def test_prompt_version_mismatch_warns_but_approves(seed_queued) -> None:
    seeded = await seed_queued(lead_status="composed", approved=False)

    async with session_scope() as session:
        await MailRepo(session).approve(
            seeded.lead_id,
            expected_footer_version=FOOTER_VERSION,
            expected_prompt_version="phase3-v999",
        )
        event = (
            await session.execute(
                select(Event).where(
                    Event.action == "approved", Event.entity_id == seeded.message_id
                )
            )
        ).scalar_one()

    assert event.detail["prompt_version_warning"] is True


async def test_unapprove_requires_queued_and_no_live_send(seed_queued) -> None:
    seeded = await seed_queued()
    async with session_scope() as session:
        await MailRepo(session).unapprove(seeded.lead_id)
    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        event = (
            await session.execute(
                select(Event).where(
                    Event.action == "unapproved", Event.entity_id == seeded.message_id
                )
            )
        ).scalar_one()
    assert lead is not None and lead.status == "composed"
    assert event.detail["lead_id"] == seeded.lead_id

    async with session_scope() as session:
        with pytest.raises(ApprovalRefused, match="not_queued"):
            await MailRepo(session).unapprove(seeded.lead_id)

    live = await seed_queued(source_id="live.pl", email="biuro@live.pl")
    async with session_scope() as session:
        session.add(
            Send(
                message_id=live.message_id,
                to_email=live.email,
                status=SendStatus.PENDING,
                mode="live",
                created_at=NOW,
            )
        )
    async with session_scope() as session:
        with pytest.raises(ApprovalRefused, match="has_live_send"):
            await MailRepo(session).unapprove(live.lead_id)


async def test_mark_lead_replied_and_suppressed_are_audited(seed_queued) -> None:
    seeded = await seed_queued()
    async with session_scope() as session:
        lead = await MailRepo(session).mark_lead(seeded.lead_id, "replied", reason="odpowiedź")
    assert lead.status == "replied"

    other = await seed_queued(source_id="b.pl", email="biuro@b.pl")
    async with session_scope() as session:
        await MailRepo(session).mark_lead(other.lead_id, "suppressed", reason="opt-out")
    async with session_scope() as session:
        suppression = (
            await session.execute(select(Suppression).where(Suppression.value == "biuro@b.pl"))
        ).scalar_one()
        events = list(
            (await session.execute(select(Event).where(Event.action == "marked"))).scalars()
        )
    assert suppression.kind == "email"
    assert {event.detail["status"] for event in events} == {"replied", "suppressed"}


async def test_mark_lead_sent_requires_a_real_send(seed_queued) -> None:
    seeded = await seed_queued()
    async with session_scope() as session:
        with pytest.raises(ApprovalRefused, match="no_sent_or_uncertain_send"):
            await MailRepo(session).mark_lead(seeded.lead_id, "sent")


async def test_mark_send_resolves_pending_and_uncertain(seed_queued) -> None:
    seeded = await seed_queued()
    async with session_scope() as session:
        send = Send(
            message_id=seeded.message_id,
            to_email=seeded.email,
            status=SendStatus.PENDING,
            mode="live",
            created_at=NOW,
        )
        session.add(send)
        await session.flush()
        send_id = send.id

    async with session_scope() as session:
        row = await MailRepo(session).mark_send(send_id, "sent")
    assert row.status == SendStatus.SENT
    assert row.sent_at == NOW

    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
    assert lead is not None and lead.status == "sent"

    uncertain = await seed_queued(source_id="b.pl", email="biuro@b.pl")
    async with session_scope() as session:
        send = Send(
            message_id=uncertain.message_id,
            to_email=uncertain.email,
            status=SendStatus.UNCERTAIN,
            mode="live",
            created_at=NOW,
        )
        session.add(send)
        await session.flush()
        send_id = send.id

    async with session_scope() as session:
        row = await MailRepo(session).mark_send(send_id, "failed", reason="operator")
    assert row.status == SendStatus.FAILED
    async with session_scope() as session:
        lead = await session.get(Lead, uncertain.lead_id)
    # A failure never downgrades a queued lead.
    assert lead is not None and lead.status == "queued"


async def test_unsuppress_deletes_row_and_preserves_reason(seed_queued) -> None:
    async with session_scope() as session:
        await ComposeRepo(session).create_suppression(
            value="biuro@a.pl", kind="email", reason="complaint"
        )
    async with session_scope() as session:
        row = await MailRepo(session).unsuppress(
            value="BIURO@A.PL", kind="email", reason="false positive"
        )

    assert row.reason == "complaint"
    async with session_scope() as session:
        assert (await session.execute(select(Suppression))).scalar_one_or_none() is None
        event = (
            await session.execute(select(Event).where(Event.action == "unsuppressed"))
        ).scalar_one()
    assert event.detail["reason"] == "complaint"
    assert event.detail["note"] == "false positive"


async def test_recover_pending_and_daily_cap_counting(seed_queued) -> None:
    seeded = await seed_queued()
    later = NOW + timedelta(hours=2)
    async with session_scope() as session:
        session.add(
            Send(
                message_id=seeded.message_id,
                to_email=seeded.email,
                status=SendStatus.PENDING,
                mode="live",
                created_at=NOW,
            )
        )
        await session.flush()
        recovered = await MailRepo(session).recover_pending()
    assert recovered == 1

    async with session_scope() as session:
        repo = MailRepo(session)
        assert await repo.count_uncertain() == 1
        assert await repo.count_sent_between(NOW - timedelta(hours=1), later) == 1
        assert await repo.count_sent_between(later, later + timedelta(hours=1)) == 0


async def test_suppression_lookup_is_value_only(db) -> None:
    """``uq_suppressions_value`` is on ``value`` alone (Phase 4.5 P1)."""
    async with session_scope() as session:
        first = await MailRepo(session)._suppress("biuro@a.pl", "email", "manual")
    assert first.kind == "email"

    # The old ``(value, kind)`` lookup missed the existing row and then hit the
    # unique constraint. Same value + different kind must not raise.
    async with session_scope() as session:
        second = await MailRepo(session)._suppress("biuro@a.pl", "domain", "manual")
    assert second.kind == "email"


async def test_unsuppress_refuses_a_kind_mismatch(db) -> None:
    async with session_scope() as session:
        await MailRepo(session)._suppress("biuro@a.pl", "email", "manual")

    async with session_scope() as session:
        with pytest.raises(LookupError):
            await MailRepo(session).unsuppress(value="biuro@a.pl", kind="domain")


async def test_finalize_never_downgrades_a_reply(seed_queued) -> None:
    """A reply landing between selection and finalize must survive (P2)."""
    seeded = await seed_queued()
    async with session_scope() as session:
        send = Send(
            message_id=seeded.message_id,
            to_email=seeded.email,
            status=SendStatus.PENDING,
            mode="live",
            created_at=NOW,
        )
        session.add(send)
        lead = await session.get(Lead, seeded.lead_id)
        assert lead is not None
        lead.status = "replied"
        await session.flush()
        send_id = send.id

    async with session_scope() as session:
        await MailRepo(session).finalize(
            send_id, status=SendStatus.SENT, stage="data", code=250, now=NOW
        )

    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        assert lead is not None
        assert lead.status == "replied"


async def test_mark_send_never_downgrades_a_suppression(seed_queued) -> None:
    seeded = await seed_queued()
    async with session_scope() as session:
        send = Send(
            message_id=seeded.message_id,
            to_email=seeded.email,
            status=SendStatus.UNCERTAIN,
            mode="live",
            created_at=NOW,
        )
        session.add(send)
        lead = await session.get(Lead, seeded.lead_id)
        assert lead is not None
        lead.status = "suppressed"
        await session.flush()
        send_id = send.id

    async with session_scope() as session:
        await MailRepo(session).mark_send(send_id, SendStatus.SENT, now=NOW)

    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        assert lead is not None
        assert lead.status == "suppressed"
