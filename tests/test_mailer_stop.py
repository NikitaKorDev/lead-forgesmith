from __future__ import annotations

import asyncio

from sqlalchemy import select

from forgesmith import cli
from forgesmith.db.models import Send
from forgesmith.db.session import session_scope
from forgesmith.mailer.models import SendStatus
from forgesmith.mailer.runner import STOP_POLL_SECONDS, send_queued
from forgesmith.mailer.transport import FakeTransport


def _sends() -> list[Send]:
    async def read() -> list[Send]:
        async with session_scope() as session:
            return list((await session.execute(select(Send))).scalars())

    return asyncio.run(read())


async def _two(seed_queued) -> None:
    await seed_queued(source_id="a.pl", email="biuro@a.pl", domain="a.pl")
    await seed_queued(source_id="b.pl", email="biuro@b.pl", domain="b.pl")


def test_stop_before_first_message_sends_nothing(db, mail_env, seed_queued) -> None:
    asyncio.run(_two(seed_queued))
    transport = FakeTransport()

    stats = asyncio.run(
        send_queued(
            live=True,
            transport=transport,
            no_delay=True,
            confirm=lambda snapshots: True,
            stop=lambda: True,
        )
    )

    assert stats.stop_reason == "signal"
    assert stats.sent == 0
    assert transport.calls == []
    assert _sends() == []


def test_stop_during_jitter_keeps_the_finalized_prefix(
    db, mail_env, seed_queued, monkeypatch
) -> None:
    asyncio.run(_two(seed_queued))
    monkeypatch.setattr(mail_env, "mail_delay_min", 30.0)
    monkeypatch.setattr(mail_env, "mail_delay_max", 30.0)
    monkeypatch.setattr(mail_env, "mail_delay_mean", 30.0)
    monkeypatch.setattr(mail_env, "mail_delay_sigma", 0.0)

    stopped = {"value": False}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    transport = FakeTransport()
    transport.hooks.append(lambda: stopped.__setitem__("value", True))

    stats = asyncio.run(
        send_queued(
            live=True,
            transport=transport,
            sleep=fake_sleep,
            confirm=lambda snapshots: True,
            stop=lambda: stopped["value"],
        )
    )

    assert stats.stop_reason == "signal"
    assert stats.sent == 1
    assert sleeps == []  # the stop was seen before the first chunk
    sends = _sends()
    assert [row.status for row in sends] == [SendStatus.SENT]


def test_stop_is_not_observed_inside_deliver(db, mail_env, seed_queued) -> None:
    """A signal that arrives mid-``deliver`` must not cancel the attempt."""
    asyncio.run(seed_queued())
    stopped = {"value": False}
    transport = FakeTransport()
    transport.hooks.append(lambda: stopped.__setitem__("value", True))

    stats = asyncio.run(
        send_queued(
            live=True,
            transport=transport,
            no_delay=True,
            confirm=lambda snapshots: True,
            stop=lambda: stopped["value"],
        )
    )

    assert len(transport.calls) == 1
    assert stats.sent == 1
    assert [row.status for row in _sends()] == [SendStatus.SENT]


def test_stop_poll_interval_fits_a_container_grace_period() -> None:
    assert 0 < STOP_POLL_SECONDS <= 5.0


def test_signal_handler_only_sets_a_flag() -> None:
    cli._STOP = False

    cli._request_stop(15, None)

    assert cli._stop_requested() is True
    cli._STOP = False
