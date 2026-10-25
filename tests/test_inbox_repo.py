from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from forgesmith.db.models import Event, Lead, MailboxState, Send, Suppression
from forgesmith.db.repo import InboxRepo
from forgesmith.db.session import session_scope
from forgesmith.inbox.models import InboundKind, InboundStatus, ParsedInbound, RawMessage

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
MAILBOX = "imap.example.net/jan@example.com/INBOX"


def _parsed(kind: InboundKind, key: str, **kwargs: object) -> ParsedInbound:
    return ParsedInbound(kind=kind, dedupe_hash=key, size=100, **kwargs)


def _raw(uid: int, *, mailbox: str = MAILBOX, uid_validity: int = 1) -> RawMessage:
    return RawMessage(
        mailbox=mailbox,
        uid=uid,
        uid_validity=uid_validity,
        headers=b"",
        body=None,
        size=100,
    )


def _dsn(
    seeded,
    *,
    status: str = "5.1.1",
    key: str = "a" * 64,
    reported: str | None = "<orig@example.com>",
):
    return _parsed(
        InboundKind.DSN,
        key,
        final_recipient=seeded.email,
        enhanced_status=status,
        smtp_code=550,
        action="failed",
        reported_message_id=reported,
        detail={"diagnostic": f"smtp; 550 {status} rejected"},
    )


def _reply(*, key: str = "b" * 64, from_addr: str = "anna@a.pl", references: tuple[str, ...] = ()):
    return _parsed(
        InboundKind.REPLY,
        key,
        from_addr=from_addr,
        referenced_ids=references,
    )


async def _add_send(
    seeded,
    *,
    status: str = "sent",
    smtp_message_id: str = "<orig@example.com>",
    to_email: str | None = None,
) -> int:
    async with session_scope() as session:
        send = Send(
            message_id=seeded.message_id,
            to_email=to_email or seeded.email,
            status=status,
            mode="live",
            smtp_message_id=smtp_message_id,
            created_at=NOW,
        )
        session.add(send)
        await session.flush()
        return send.id


# ------------------------------------------------------------- mailbox state
async def test_prepare_mailbox_resets_only_when_uidvalidity_changes(db) -> None:
    async with session_scope() as session:
        repo = InboxRepo(session)
        state, reset = await repo.prepare_mailbox(MAILBOX, 10, now=NOW)
        assert reset is False
        assert state.uid_validity == 10
        await repo.advance_cursor(MAILBOX, 7, now=NOW)

    async with session_scope() as session:
        repo = InboxRepo(session)
        state, reset = await repo.prepare_mailbox(MAILBOX, 10, now=NOW)
        assert reset is False
        assert state.last_uid == 7
        state, reset = await repo.prepare_mailbox(MAILBOX, 11, now=NOW)
        assert reset is True
        assert state.last_uid == 0

    async with session_scope() as session:
        event = (
            await session.execute(select(Event).where(Event.action == "uidvalidity_changed"))
        ).scalar_one()
    assert event.detail["from"] == 10
    assert event.detail["to"] == 11


async def test_cursor_never_rewinds(db) -> None:
    async with session_scope() as session:
        repo = InboxRepo(session)
        await repo.prepare_mailbox(MAILBOX, 1, now=NOW)
        assert await repo.advance_cursor(MAILBOX, 5, now=NOW) is True
        assert await repo.advance_cursor(MAILBOX, 4, now=NOW) is False
        assert await repo.advance_cursor(MAILBOX, 5, now=NOW) is False

    async with session_scope() as session:
        state = (await session.execute(select(MailboxState))).scalar_one()
    assert state.last_uid == 5


# ---------------------------------------------------------------- attribution
async def test_hard_dsn_is_attributed_by_message_id(seed_queued) -> None:
    seeded = await seed_queued()
    send_id = await _add_send(seeded)
    parsed = _dsn(seeded)

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert row.send_id == send_id
        assert row.lead_id == seeded.lead_id
        assert row.status == InboundStatus.NEW
        assert repo.should_auto_apply(parsed, row) is True


async def test_hard_dsn_is_attributed_by_recipient_without_message_id(seed_queued) -> None:
    seeded = await seed_queued()
    send_id = await _add_send(seeded, smtp_message_id="<other@example.com>")
    parsed = _dsn(seeded, reported=None)

    async with session_scope() as session:
        row = await InboxRepo(session).record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert row.send_id == send_id
        assert row.detail["attribution"]["method"] == "recipient"


async def test_soft_dsn_is_ignored(seed_queued) -> None:
    seeded = await seed_queued()
    await _add_send(seeded)
    parsed = _dsn(seeded, status="4.2.2")

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert row.status == InboundStatus.IGNORED
        assert repo.should_auto_apply(parsed, row) is False


async def test_thread_reply_is_attributed_and_marks_the_lead_replied(seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    send_id = await _add_send(seeded)
    parsed = _reply(references=("orig@example.com",))

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert row.lead_id == seeded.lead_id
        assert row.send_id == send_id
        assert await repo.apply_inbound(row, now=NOW) is True

    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        event = (
            await session.execute(select(Event).where(Event.action == "inbound_reply"))
        ).scalar_one()
    assert lead is not None
    assert lead.status == "replied"
    assert event.detail["source"] == "imap"


async def test_reply_falls_back_to_contact_email(seed_queued) -> None:
    seeded = await seed_queued(email="anna@a.pl")
    send_id = await _add_send(seeded)
    parsed = _reply(from_addr="anna@a.pl", references=())

    async with session_scope() as session:
        row = await InboxRepo(session).record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert row.lead_id == seeded.lead_id
        assert row.send_id == send_id
        assert row.detail["attribution"]["method"] == "contact"


async def test_reply_falls_back_to_domain(seed_queued) -> None:
    seeded = await seed_queued(email="biuro@a.pl", domain="a.pl")
    await _add_send(seeded)
    parsed = _reply(from_addr="ktos@a.pl", references=())

    async with session_scope() as session:
        row = await InboxRepo(session).record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert row.lead_id == seeded.lead_id
        assert row.detail["attribution"]["method"] == "domain"


async def test_unmatched_reply_needs_review(seed_queued) -> None:
    seeded = await seed_queued(email="biuro@a.pl", domain="a.pl")
    await _add_send(seeded)
    parsed = _reply(from_addr="ktos@inny.pl", references=())

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert row.status == InboundStatus.NEEDS_REVIEW
        assert repo.should_auto_apply(parsed, row) is False


# ------------------------------------------------------------------ effects
async def test_hard_dsn_on_an_accepted_send_suppresses_only_the_address(seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    send_id = await _add_send(seeded)
    parsed = _dsn(seeded, status="5.1.2")

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert await repo.apply_inbound(row, now=NOW) is True

    async with session_scope() as session:
        send = await session.get(Send, send_id)
        lead = await session.get(Lead, seeded.lead_id)
        suppressions = list((await session.execute(select(Suppression))).scalars())
        event = (
            await session.execute(select(Event).where(Event.action == "dsn_bounce"))
        ).scalar_one()
    assert send is not None and send.status == "sent"  # the relay accepted it
    assert lead is not None and lead.status == "bounced"
    assert [(row.kind, row.value) for row in suppressions] == [("email", seeded.email)]
    assert suppressions[0].reason == "dsn:5.1.2"
    assert event.detail["inbound_message_id"] == row.id


async def test_hard_dsn_on_a_pending_send_is_finalized_without_a_domain_suppression(
    seed_queued,
) -> None:
    seeded = await seed_queued()
    send_id = await _add_send(seeded, status="pending")
    parsed = _dsn(seeded, status="5.1.2")

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert await repo.apply_inbound(row, now=NOW) is True

    async with session_scope() as session:
        send = await session.get(Send, send_id)
        suppressions = list((await session.execute(select(Suppression))).scalars())
    assert send is not None
    assert send.status == "bounced"
    assert send.error is not None and "dsn" in send.error
    # A DSN names one address; it must never poison the whole domain.
    assert [row.kind for row in suppressions] == ["email"]


async def test_apply_inbound_is_idempotent(seed_queued) -> None:
    seeded = await seed_queued()
    await _add_send(seeded)
    parsed = _dsn(seeded)

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        row_id = row.id
        assert await repo.apply_inbound(row, now=NOW) is True

    async with session_scope() as session:
        repo = InboxRepo(session)
        again = await repo.get_inbound(row_id)
        assert again is not None
        assert await repo.apply_inbound(again, now=NOW) is False


async def test_optout_requires_an_explicit_confirmation(seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    await _add_send(seeded)
    parsed = _parsed(
        InboundKind.OPTOUT,
        "c" * 64,
        from_addr="anna@a.pl",
        referenced_ids=("orig@example.com",),
        optout_phrase="prosze nie kontaktowac",
        detail={"optout_phrase": "prosze nie kontaktowac"},
    )

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert row.status == InboundStatus.NEEDS_REVIEW
        row_id = row.id
        assert await repo.apply_inbound(row, now=NOW) is False

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.get_inbound(row_id)
        assert row is not None
        assert await repo.apply_inbound(row, now=NOW, allow_optout=True) is True

    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        suppression = (await session.execute(select(Suppression))).scalar_one()
        event = (
            await session.execute(select(Event).where(Event.action == "inbound_optout"))
        ).scalar_one()
    assert lead is not None and lead.status == "suppressed"
    assert suppression.value == "anna@a.pl"
    assert suppression.kind == "email"
    assert suppression.reason.startswith("optout:")
    assert event.detail["phrase"] == "prosze nie kontaktowac"


async def test_suppressed_lead_is_not_downgraded_by_a_reply(seed_queued) -> None:
    seeded = await seed_queued(lead_status="suppressed")
    await _add_send(seeded)
    parsed = _reply(references=("orig@example.com",))

    async with session_scope() as session:
        repo = InboxRepo(session)
        row = await repo.record_inbound(parsed, _raw(1), now=NOW)
        assert row is not None
        assert await repo.apply_inbound(row, now=NOW) is True

    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
    assert lead is not None and lead.status == "suppressed"
