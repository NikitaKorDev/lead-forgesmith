from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from forgesmith.db.models import Event, Lead, Send, Suppression
from forgesmith.db.session import session_scope
from forgesmith.mailer.models import SendStatus
from forgesmith.mailer.runner import send_queued
from forgesmith.mailer.transport import FakeTransport, SendOutcome

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def _outcome(
    status: str,
    *,
    stage: str = "data",
    code: int | None = 250,
    response: str | None = "250 OK",
    enhanced: str | None = None,
) -> SendOutcome:
    return SendOutcome(
        status=status,
        stage=stage,
        code=code,
        response=response,
        enhanced=enhanced,
    )


async def _seed_attempts(message_id: int, email: str, count: int) -> None:
    async with session_scope() as session:
        for index in range(count):
            session.add(
                Send(
                    message_id=message_id,
                    to_email=email,
                    status=SendStatus.FAILED,
                    mode="live",
                    created_at=NOW - timedelta(hours=count - index),
                )
            )


async def test_dry_run_writes_eml_without_a_sends_row(seed_queued, mail_env, mail_sender) -> None:
    seeded = await seed_queued()

    stats = await send_queued(settings=mail_env, sender=mail_sender, now=lambda: NOW, live=False)

    assert (stats.scanned, stats.dry_run, stats.sent) == (1, 1, 0)
    path = mail_env.outbox_dir / f"lead{seeded.lead_id}-msg{seeded.message_id}.eml"
    assert path.exists()
    assert b"Content-Transfer-Encoding: quoted-printable" in path.read_bytes()

    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        sends = (await session.execute(select(func.count()).select_from(Send))).scalar_one()
        event = (await session.execute(select(Event).where(Event.action == "dry_run"))).scalar_one()
    assert lead is not None and lead.status == "queued"
    assert sends == 0
    assert event.detail["lead_id"] == seeded.lead_id


async def test_a_live_run_still_selects_a_dry_run_lead(seed_queued, mail_env, mail_sender) -> None:
    await seed_queued()
    await send_queued(settings=mail_env, sender=mail_sender, now=lambda: NOW, live=False)

    transport = FakeTransport()
    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert stats.sent == 1
    assert transport.opened == 1
    assert transport.closed == 1
    assert len(transport.calls) == 1


async def test_success_marks_send_sent_and_lead_sent(seed_queued, mail_env, mail_sender) -> None:
    seeded = await seed_queued()
    transport = FakeTransport([_outcome(SendStatus.SENT)])

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert stats.sent == 1
    async with session_scope() as session:
        send = (await session.execute(select(Send))).scalar_one()
        lead = await session.get(Lead, seeded.lead_id)
        events = list(
            (await session.execute(select(Event).where(Event.entity == "send"))).scalars()
        )
    assert send.status == SendStatus.SENT
    assert send.sent_at is not None
    assert send.smtp_message_id
    assert lead is not None and lead.status == "sent"
    assert {event.action for event in events} == {"send_attempt", "sent"}


async def test_transient_failure_retries_only_after_backoff(
    seed_queued, mail_env, mail_sender
) -> None:
    seeded = await seed_queued()
    transport = FakeTransport(
        [_outcome(SendStatus.FAILED, code=451, response="451 4.7.1 greylisted")]
    )

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )
    assert stats.failed == 1

    async with session_scope() as session:
        send = (await session.execute(select(Send))).scalar_one()
        lead = await session.get(Lead, seeded.lead_id)
    assert send.status == SendStatus.FAILED
    assert lead is not None and lead.status == "queued"

    blocked = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=FakeTransport(),
        now=lambda: NOW,
    )
    assert blocked.deferred == 1
    assert blocked.sent == 0

    retried = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=FakeTransport([_outcome(SendStatus.SENT)]),
        now=lambda: NOW + timedelta(minutes=6),
    )
    assert retried.sent == 1


async def test_mailbox_rejection_bounces_and_suppresses(seed_queued, mail_env, mail_sender) -> None:
    seeded = await seed_queued()
    transport = FakeTransport(
        [
            _outcome(
                SendStatus.BOUNCED,
                stage="rcpt",
                code=550,
                response="550 5.1.1 mailbox unavailable",
                enhanced="5.1.1",
            )
        ]
    )

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert stats.bounced == 1
    async with session_scope() as session:
        lead = await session.get(Lead, seeded.lead_id)
        suppression = (
            await session.execute(select(Suppression).where(Suppression.value == seeded.email))
        ).scalar_one()
    assert lead is not None and lead.status == "bounced"
    assert suppression.kind == "email"


async def test_bare_550_fails_without_suppressing(seed_queued, mail_env, mail_sender) -> None:
    await seed_queued()
    transport = FakeTransport(
        [
            _outcome(
                SendStatus.FAILED,
                stage="rcpt",
                code=550,
                response="550 rejected",
            )
        ]
    )

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert stats.failed == 1
    async with session_scope() as session:
        assert (
            await session.execute(select(func.count()).select_from(Suppression))
        ).scalar_one() == 0


async def test_two_policy_rejections_abort_the_run(seed_queued, mail_env, mail_sender) -> None:
    await seed_queued()
    await seed_queued(source_id="b.pl", email="biuro@b.pl")
    rejection = _outcome(
        SendStatus.FAILED,
        stage="data",
        code=554,
        response="554 5.7.1 message rejected as spam",
        enhanced="5.7.1",
    )
    transport = FakeTransport([rejection, rejection])

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        no_delay=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert stats.failed == 2
    assert stats.stop_reason == "policy_rejection_streak"


async def test_pre_mail_connection_loss_is_aborted_not_an_attempt(
    seed_queued, mail_env, mail_sender
) -> None:
    seeded = await seed_queued()
    transport = FakeTransport(
        [_outcome(SendStatus.ABORTED, stage="mail", code=None, response=None)]
    )

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert stats.aborted == 1
    assert stats.stop_reason == "aborted"
    async with session_scope() as session:
        send = (await session.execute(select(Send))).scalar_one()
        lead = await session.get(Lead, seeded.lead_id)
    assert send.status == SendStatus.ABORTED
    assert lead is not None and lead.status == "queued"


async def test_jitter_between_live_sends_and_none_for_dry(
    seed_queued, mail_env, mail_sender, monkeypatch
) -> None:
    await seed_queued()
    await seed_queued(source_id="b.pl", email="biuro@b.pl")
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=False,
        now=lambda: NOW,
        sleep=fake_sleep,
    )
    assert stats.dry_run == 2
    assert sleeps == []

    monkeypatch.setattr(mail_env, "mail_delay_mean", 10_000.0)
    monkeypatch.setattr(mail_env, "mail_delay_min", 120.0)
    monkeypatch.setattr(mail_env, "mail_delay_max", 1800.0)
    transport = FakeTransport([_outcome(SendStatus.SENT), _outcome(SendStatus.SENT)])
    async with session_scope() as session:
        # A dry run left both leads queued and selectable.
        assert len(list((await session.execute(select(Lead))).scalars())) == 2

    await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW + timedelta(days=1),
        sleep=fake_sleep,
        rng=random.Random(1),
    )
    assert sleeps == [1800.0]


async def test_unexpected_transport_exception_becomes_uncertain(
    seed_queued, mail_env, mail_sender
) -> None:
    seeded = await seed_queued()
    other = await seed_queued(source_id="b.pl", domain="b.pl", email="biuro@b.pl")
    transport = FakeTransport([RuntimeError("socket vanished mid-DATA")])

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert (stats.uncertain, stats.stop_reason) == (1, "transport_error")
    # The run stops after the unclassifiable failure: the next recipient was
    # not turned into collateral uncertainty.
    assert len(transport.calls) == 1
    async with session_scope() as session:
        sends = list((await session.execute(select(Send))).scalars())
        lead = await session.get(Lead, seeded.lead_id)
        untouched = await session.get(Lead, other.lead_id)
    assert len(sends) == 1
    assert sends[0].status == SendStatus.UNCERTAIN
    assert "socket vanished" in (sends[0].error or "")
    assert lead is not None and lead.status == "queued"
    assert untouched is not None and untouched.status == "queued"

    # Uncertain is never retried automatically: the next run skips it and
    # still sends the untouched recipient.
    second = FakeTransport([_outcome(SendStatus.SENT)])
    follow_up = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=second,
        now=lambda: NOW + timedelta(hours=1),
    )
    assert (follow_up.sent, follow_up.skipped) == (1, 1)
    assert [call[1] for call in second.calls] == [other.email]


async def test_archive_failure_aborts_without_transmitting(
    seed_queued, mail_env, mail_sender, monkeypatch
) -> None:
    seeded = await seed_queued()

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("forgesmith.mailer.runner.write_eml", failing_write)
    transport = FakeTransport()

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert stats.aborted == 1
    assert stats.stop_reason == "archive_failed"
    assert transport.calls == []
    async with session_scope() as session:
        send = (await session.execute(select(Send))).scalar_one()
        lead = await session.get(Lead, seeded.lead_id)
    assert send.status == SendStatus.ABORTED
    assert "archive_failed" in (send.error or "")
    assert lead is not None and lead.status == "queued"


async def test_dry_run_archive_failure_is_reported_not_raised(
    seed_queued, mail_env, mail_sender, monkeypatch
) -> None:
    await seed_queued()

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("forgesmith.mailer.runner.write_eml", failing_write)

    stats = await send_queued(settings=mail_env, sender=mail_sender, live=False, now=lambda: NOW)

    assert (stats.dry_run, stats.skipped, stats.stop_reason) == (0, 1, "archive_failed")
    async with session_scope() as session:
        sends = (await session.execute(select(func.count()).select_from(Send))).scalar_one()
    assert sends == 0


async def test_poison_lead_does_not_starve_the_queue(seed_queued, mail_env, mail_sender) -> None:
    poison = await seed_queued()
    fresh = await seed_queued(source_id="b.pl", email="biuro@b.pl")
    await _seed_attempts(poison.message_id, poison.email, 3)

    transport = FakeTransport([_outcome(SendStatus.SENT)])
    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
        limit=1,
    )

    assert stats.sent == 1
    assert transport.calls[0][1] == fresh.email


async def test_a_consent_country_is_skipped_without_an_attempt(
    seed_queued, mail_env, mail_sender
) -> None:
    seeded = await seed_queued(country="DE")
    transport = FakeTransport([_outcome(SendStatus.SENT)])

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert (stats.sent, stats.skipped, stats.failed) == (0, 1, 0)
    assert transport.calls == []
    async with session_scope() as session:
        sends = (await session.execute(select(func.count()).select_from(Send))).scalar_one()
        lead = await session.get(Lead, seeded.lead_id)
    assert sends == 0
    assert lead is not None and lead.status == "queued"


async def test_a_closed_country_window_defers_and_sends_later(
    seed_queued, mail_env, mail_sender, monkeypatch
) -> None:
    monkeypatch.setattr(mail_env, "mail_window_start", 9)
    monkeypatch.setattr(mail_env, "mail_window_end", 17)
    monkeypatch.setattr(mail_env, "mail_countries", "US")
    seeded = await seed_queued(country="US")
    transport = FakeTransport([_outcome(SendStatus.SENT)])

    # 11:00 in Warsaw, 05:00 in New York: the outer window is open, the
    # recipient's own window is not.
    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert (stats.sent, stats.deferred, stats.failed) == (0, 1, 0)
    assert transport.calls == []
    async with session_scope() as session:
        sends = (await session.execute(select(func.count()).select_from(Send))).scalar_one()
        lead = await session.get(Lead, seeded.lead_id)
    assert sends == 0
    assert lead is not None and lead.status == "queued"

    later = NOW + timedelta(hours=4)  # 09:00 in New York
    sent = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=FakeTransport([_outcome(SendStatus.SENT)]),
        now=lambda: later,
    )

    assert (sent.sent, sent.deferred) == (1, 0)
