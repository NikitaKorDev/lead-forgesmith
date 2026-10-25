from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from forgesmith.db.models import Send
from forgesmith.db.session import session_scope
from forgesmith.mailer.runner import send_queued
from forgesmith.mailer.transport import FakeTransport

SATURDAY_NOON = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _send_count() -> int:
    async def read() -> int:
        async with session_scope() as session:
            return len(list((await session.execute(select(Send))).scalars()))

    return asyncio.run(read())


def test_closed_window_is_checked_before_smtp_connect(
    db, mail_env, seed_queued, monkeypatch
) -> None:
    asyncio.run(seed_queued())
    monkeypatch.setattr(mail_env, "mail_window_start", 9)
    monkeypatch.setattr(mail_env, "mail_window_end", 17)
    monkeypatch.setattr(mail_env, "mail_weekdays_only", True)
    transport = FakeTransport()

    stats = asyncio.run(
        send_queued(
            live=True,
            transport=transport,
            confirm=lambda snapshots: True,
            now=lambda: SATURDAY_NOON,
        )
    )

    assert stats.stop_reason == "outside_window"
    assert stats.scanned == 1
    assert transport.opened == 0
    assert transport.calls == []
    assert _send_count() == 0


def test_daily_cap_is_checked_before_smtp_connect(db, mail_env, seed_queued, monkeypatch) -> None:
    asyncio.run(seed_queued())
    monkeypatch.setattr(mail_env, "mail_daily_cap", 0)
    transport = FakeTransport()

    stats = asyncio.run(
        send_queued(
            live=True,
            transport=transport,
            confirm=lambda snapshots: True,
            now=lambda: SATURDAY_NOON,
        )
    )

    assert stats.stop_reason == "daily_cap"
    assert transport.opened == 0
    assert transport.calls == []
    assert _send_count() == 0
