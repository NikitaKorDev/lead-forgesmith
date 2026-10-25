from __future__ import annotations

import asyncio

from sqlalchemy import select
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.config import settings
from forgesmith.db.models import Send
from forgesmith.db.session import session_scope
from forgesmith.mailer.runner import send_queued
from forgesmith.mailer.transport import FakeTransport

runner = CliRunner()


def _sends() -> list[Send]:
    async def read() -> list[Send]:
        async with session_scope() as session:
            return list((await session.execute(select(Send))).scalars())

    return asyncio.run(read())


def test_kill_switch_blocks_live_before_transport(db, mail_env, seed_queued, monkeypatch) -> None:
    asyncio.run(seed_queued())
    monkeypatch.setattr(settings, "mail_dry_run", True)
    transport = FakeTransport()

    stats = asyncio.run(send_queued(live=True, transport=transport))

    assert stats.stop_reason == "kill_switch"
    assert stats.sent == 0
    assert transport.opened == 0
    assert transport.calls == []
    assert _sends() == []
    assert not list(mail_env.outbox_dir.glob("*.eml"))


def test_kill_switch_does_not_block_dry_runs(db, mail_env, seed_queued, monkeypatch) -> None:
    seeded = asyncio.run(seed_queued())
    monkeypatch.setattr(settings, "mail_dry_run", True)

    stats = asyncio.run(send_queued(live=False))

    assert stats.stop_reason is None
    assert stats.dry_run == 1
    assert (mail_env.outbox_dir / f"lead{seeded.lead_id}-msg{seeded.message_id}.eml").exists()


def test_cli_live_send_exits_zero_when_paused(db, mail_env, seed_queued, monkeypatch) -> None:
    asyncio.run(seed_queued())
    monkeypatch.setattr(settings, "mail_dry_run", True)

    async def with_fake(**kwargs):
        return await send_queued(transport=FakeTransport(), **kwargs)

    monkeypatch.setattr(cli, "send_queued", with_fake)
    result = runner.invoke(cli.app, ["send", "--live", "--yes", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "Kill switch armed" in result.output
    assert "kill_switch" in result.output
    assert _sends() == []


def test_cli_plain_send_still_dry_runs_when_paused(db, mail_env, seed_queued, monkeypatch) -> None:
    seeded = asyncio.run(seed_queued())
    monkeypatch.setattr(settings, "mail_dry_run", True)

    result = runner.invoke(cli.app, ["send"])

    assert result.exit_code == 0, result.output
    assert (mail_env.outbox_dir / f"lead{seeded.lead_id}-msg{seeded.message_id}.eml").exists()
