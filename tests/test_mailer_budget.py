from __future__ import annotations

import asyncio

from sqlalchemy import select
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.db.models import Send
from forgesmith.db.session import session_scope
from forgesmith.mailer.models import SendStats
from forgesmith.mailer.runner import send_queued
from forgesmith.mailer.transport import FakeTransport

runner = CliRunner()


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def _two(seed_queued) -> None:
    await seed_queued(source_id="a.pl", email="biuro@a.pl", domain="a.pl")
    await seed_queued(source_id="b.pl", email="biuro@b.pl", domain="b.pl")


def _send_count() -> int:
    async def read() -> int:
        async with session_scope() as session:
            return len(list((await session.execute(select(Send))).scalars()))

    return asyncio.run(read())


def test_budget_stops_between_messages(db, mail_env, seed_queued) -> None:
    asyncio.run(_two(seed_queued))
    clock = FakeClock()
    transport = FakeTransport()
    transport.hooks.append(lambda: clock.advance(100))

    stats = asyncio.run(
        send_queued(
            live=True,
            transport=transport,
            no_delay=True,
            confirm=lambda snapshots: True,
            clock=clock.now,
            max_runtime=50,
        )
    )

    assert stats.stop_reason == "budget"
    assert stats.sent == 1
    assert _send_count() == 1


def test_jitter_is_capped_to_the_remaining_budget(db, mail_env, seed_queued, monkeypatch) -> None:
    asyncio.run(_two(seed_queued))
    monkeypatch.setattr(mail_env, "mail_delay_min", 30.0)
    monkeypatch.setattr(mail_env, "mail_delay_max", 30.0)
    monkeypatch.setattr(mail_env, "mail_delay_mean", 30.0)
    monkeypatch.setattr(mail_env, "mail_delay_sigma", 0.0)

    clock = FakeClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    stats = asyncio.run(
        send_queued(
            live=True,
            transport=FakeTransport(),
            sleep=fake_sleep,
            confirm=lambda snapshots: True,
            clock=clock.now,
            max_runtime=10,
        )
    )

    assert sleeps == [10.0]
    assert stats.stop_reason == "budget"
    assert stats.sent == 1


def test_cli_passes_max_runtime_and_stop(db, mail_env, monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_send(**kwargs):
        captured.update(kwargs)
        return SendStats()

    monkeypatch.setattr(cli, "send_queued", fake_send)
    result = runner.invoke(cli.app, ["send", "--live", "--yes", "--max-runtime", "12.5"])

    assert result.exit_code == 0, result.output
    assert captured["max_runtime"] == 12.5
    assert callable(captured["stop"])
    assert callable(captured["confirm"])
