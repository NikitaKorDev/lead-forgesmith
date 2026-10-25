from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from forgesmith.db.models import InboundMessage, Lead, MailboxState, Send, Suppression
from forgesmith.db.session import session_scope
from forgesmith.inbox.models import RawMessage
from forgesmith.inbox.runner import sync_inbox
from forgesmith.inbox.source import FakeInbox
from forgesmith.mailer.lock import RunLock
from forgesmith.mailer.models import LockHeldError

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
MAILBOX = "fake/INBOX"


def _dsn_raw(
    recipient: str,
    *,
    status: str = "5.1.1",
    message_id: str = "<orig@example.com>",
    dsn_id: str = "<dsn@example.net>",
) -> bytes:
    return (
        "From: MAILER-DAEMON@example.net\r\n"
        "To: jan@example.com\r\n"
        "Subject: Undelivered Mail Returned to Sender\r\n"
        "Date: Thu, 22 Oct 2026 10:00:00 +0000\r\n"
        f"Message-ID: {dsn_id}\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n'
        "\r\n"
        "--b\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "This is the mail system.\r\n"
        "--b\r\n"
        "Content-Type: message/delivery-status\r\n"
        "\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\n"
        "Action: failed\r\n"
        f"Status: {status}\r\n"
        f"Diagnostic-Code: smtp; 550 {status} rejected\r\n"
        "--b\r\n"
        "Content-Type: message/rfc822\r\n"
        "\r\n"
        "From: jan@example.com\r\n"
        f"Message-ID: {message_id}\r\n"
        "Subject: Hi\r\n"
        "\r\n"
        "body\r\n"
        "--b--\r\n"
    ).encode()


def _reply_raw(
    *,
    from_addr: str = "anna@a.pl",
    message_id: str = "<reply@a.pl>",
    in_reply_to: str | None = "<orig@example.com>",
    body: str = "Dzi\u0119kuj\u0119 za wiadomo\u015b\u0107.",
) -> bytes:
    return (
        f"From: Anna <{from_addr}>\r\n"
        "To: jan@example.com\r\n"
        "Subject: Re: Hi\r\n"
        "Date: Thu, 22 Oct 2026 11:00:00 +0000\r\n"
        f"Message-ID: {message_id}\r\n"
        + (f"In-Reply-To: {in_reply_to}\r\n" if in_reply_to else "")
        + "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n" + body
    ).encode()


def _raw_message(uid: int, raw: bytes, *, uid_validity: int = 1) -> RawMessage:
    return RawMessage(
        mailbox=MAILBOX,
        uid=uid,
        uid_validity=uid_validity,
        headers=raw,
        body=None,
        size=len(raw),
    )


async def _add_send(
    seeded, *, status: str = "sent", smtp_message_id: str = "<orig@example.com>"
) -> int:
    async with session_scope() as session:
        send = Send(
            message_id=seeded.message_id,
            to_email=seeded.email,
            status=status,
            mode="live",
            smtp_message_id=smtp_message_id,
            created_at=NOW,
        )
        session.add(send)
        await session.flush()
        return send.id


async def test_sync_applies_a_hard_dsn_and_moves_the_cursor(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    send_id = await _add_send(seeded)
    source = FakeInbox([_raw_message(1, _dsn_raw(seeded.email))])

    stats = await sync_inbox(settings=mail_env, source=source, now=NOW)

    assert (stats.scanned, stats.ingested, stats.applied, stats.bounces) == (1, 1, 1, 1)
    assert source.opened is True and source.closed is True
    async with session_scope() as session:
        send = await session.get(Send, send_id)
        lead = await session.get(Lead, seeded.lead_id)
        state = (await session.execute(select(MailboxState))).scalar_one()
    assert send is not None and send.status == "sent"
    assert lead is not None and lead.status == "bounced"
    assert state.last_uid == 1


async def test_a_second_sync_is_a_noop(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    await _add_send(seeded)
    message = _raw_message(1, _dsn_raw(seeded.email))

    await sync_inbox(settings=mail_env, source=FakeInbox([message]), now=NOW)
    again = await sync_inbox(settings=mail_env, source=FakeInbox([message]), now=NOW)

    assert again.scanned == 0
    assert again.ingested == 0
    assert again.applied == 0


async def test_dry_run_writes_nothing(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    await _add_send(seeded)

    stats = await sync_inbox(
        settings=mail_env,
        source=FakeInbox([_raw_message(1, _dsn_raw(seeded.email))]),
        dry_run=True,
        now=NOW,
    )

    assert stats.dry_run == 1
    assert stats.ingested == 0
    async with session_scope() as session:
        assert (await session.execute(select(InboundMessage))).first() is None
        assert (await session.execute(select(MailboxState))).first() is None
        assert (await session.execute(select(Suppression))).first() is None
        lead = await session.get(Lead, seeded.lead_id)
    assert lead is not None and lead.status == "sent"


async def test_replay_after_a_cursor_reset_does_not_double_apply(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    await _add_send(seeded)
    message = _raw_message(1, _dsn_raw(seeded.email))
    await sync_inbox(settings=mail_env, source=FakeInbox([message]), now=NOW)

    async with session_scope() as session:
        state = (await session.execute(select(MailboxState))).scalar_one()
        state.last_uid = 0

    again = await sync_inbox(settings=mail_env, source=FakeInbox([message]), now=NOW)

    assert again.duplicates == 1
    assert again.applied == 0
    async with session_scope() as session:
        suppressions = list((await session.execute(select(Suppression))).scalars())
    assert len(suppressions) == 1


async def test_uidvalidity_change_rebootstraps_from_since(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    await _add_send(seeded)
    await sync_inbox(
        settings=mail_env,
        source=FakeInbox(
            [_raw_message(5, _dsn_raw(seeded.email, dsn_id="<d1@example.net>"))],
            uid_validity=1,
        ),
        now=NOW,
    )

    source = FakeInbox(
        [_raw_message(1, _dsn_raw(seeded.email, dsn_id="<d2@example.net>"))],
        uid_validity=2,
    )
    stats = await sync_inbox(settings=mail_env, source=source, now=NOW)

    assert stats.uidvalidity_reset == 1
    after_uid, _limit, since = source.fetch_calls[0]
    assert after_uid == 0
    assert since is not None


async def test_held_lock_is_refused(db, mail_env) -> None:
    lock = RunLock(mail_env.inbox_lock_path)
    lock.acquire()
    try:
        with pytest.raises(LockHeldError):
            await sync_inbox(settings=mail_env, source=FakeInbox([]), now=NOW)
    finally:
        lock.release()


async def test_pending_send_is_finalized_by_a_dsn(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued()
    send_id = await _add_send(seeded, status="pending")

    await sync_inbox(
        settings=mail_env,
        source=FakeInbox([_raw_message(1, _dsn_raw(seeded.email, status="5.1.2"))]),
        now=NOW,
    )

    async with session_scope() as session:
        send = await session.get(Send, send_id)
        suppressions = list((await session.execute(select(Suppression))).scalars())
    assert send is not None and send.status == "bounced"
    assert [row.kind for row in suppressions] == ["email"]


async def test_thread_reply_marks_the_lead_replied(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    await _add_send(seeded)

    stats = await sync_inbox(
        settings=mail_env,
        source=FakeInbox([_raw_message(1, _reply_raw())]),
        now=NOW,
    )

    assert stats.replies == 1
    assert stats.applied == 1
    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
    assert lead is not None and lead.status == "replied"


async def test_optout_is_left_for_review(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued(lead_status="sent")
    await _add_send(seeded)

    stats = await sync_inbox(
        settings=mail_env,
        source=FakeInbox(
            [
                _raw_message(
                    1,
                    _reply_raw(body="Prosz\u0119 nie kontaktowa\u0107 si\u0119 wi\u0119cej."),
                )
            ]
        ),
        now=NOW,
    )

    assert stats.needs_review == 1
    assert stats.applied == 0
    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        suppression = (await session.execute(select(Suppression))).first()
        inbound = (await session.execute(select(InboundMessage))).scalar_one()
    assert lead is not None and lead.status == "sent"
    assert suppression is None
    assert inbound.status == "needs_review"


async def test_a_uid_conflict_still_advances_the_cursor(db, mail_env) -> None:
    async with session_scope() as session:
        session.add(
            InboundMessage(
                mailbox=MAILBOX,
                uid_validity=1,
                imap_uid=1,
                dedupe_hash="x" * 64,
                kind="other",
                status="ignored",
            )
        )
        session.add(MailboxState(mailbox=MAILBOX, uid_validity=1, last_uid=0))

    source = FakeInbox([_raw_message(1, _dsn_raw("biuro@a.pl", dsn_id="<new@example.net>"))])
    stats = await sync_inbox(settings=mail_env, source=source, now=NOW)

    assert stats.duplicates == 1
    async with session_scope() as session:
        state = (await session.execute(select(MailboxState))).scalar_one()
        rows = list((await session.execute(select(InboundMessage))).scalars())
    assert state.last_uid == 1
    assert len(rows) == 1


async def test_limit_caps_the_batch(db, mail_env) -> None:
    source = FakeInbox(
        [
            _raw_message(1, _dsn_raw("a@a.pl", dsn_id="<a@example.net>")),
            _raw_message(2, _dsn_raw("b@b.pl", dsn_id="<b@example.net>")),
        ]
    )

    stats = await sync_inbox(settings=mail_env, source=source, limit=1, now=NOW)

    assert stats.scanned == 1
    assert source.fetch_calls[0][:2] == (0, 1)
