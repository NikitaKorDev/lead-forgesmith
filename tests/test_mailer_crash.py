from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from forgesmith.db.models import Lead, Send
from forgesmith.db.session import session_scope
from forgesmith.mailer.lock import RunLock
from forgesmith.mailer.models import LockHeldError, SendStatus
from forgesmith.mailer.runner import send_queued
from forgesmith.mailer.transport import FakeTransport

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


class InspectingTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.seen_statuses: list[str] = []

    async def deliver(self, **kwargs):
        async with session_scope() as session:
            rows = list((await session.execute(select(Send))).scalars())
            self.seen_statuses.extend(row.status for row in rows)
        return await super().deliver(**kwargs)


async def test_reservation_is_committed_before_the_transport_is_called(
    seed_queued, mail_env, mail_sender
) -> None:
    await seed_queued()
    transport = InspectingTransport()

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=transport,
        now=lambda: NOW,
    )

    assert stats.sent == 1
    assert transport.seen_statuses == [SendStatus.PENDING]


async def test_pending_rows_recover_to_uncertain_at_startup(
    seed_queued, mail_env, mail_sender
) -> None:
    seeded = await seed_queued()
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

    stats = await send_queued(
        settings=mail_env,
        sender=mail_sender,
        live=True,
        transport=FakeTransport(),
        now=lambda: NOW,
    )

    assert stats.uncertain_recovered == 1
    assert stats.uncertain_outstanding == 1
    assert stats.sent == 0
    assert stats.skipped == 1
    async with session_scope() as session:
        send = (await session.execute(select(Send))).scalar_one()
        lead = await session.get(Lead, seeded.lead_id)
        recovered = (
            await session.execute(select(Send).where(Send.status == SendStatus.UNCERTAIN))
        ).scalar_one()
    assert send.status == SendStatus.UNCERTAIN
    assert recovered.error == "recovered_after_crash"
    assert lead is not None and lead.status == "queued"


async def test_run_lock_refuses_a_second_holder(tmp_path) -> None:
    path = tmp_path / "mailer.lock"
    first = RunLock(path)
    first.acquire()
    try:
        second = RunLock(path)
        with pytest.raises(LockHeldError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
