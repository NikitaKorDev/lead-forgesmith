from __future__ import annotations

import asyncio

from sqlalchemy import select
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.db.models import Event
from forgesmith.db.session import session_scope

runner = CliRunner()


def _runs() -> list[Event]:
    async def read() -> list[Event]:
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(Event).where(Event.entity == "run").order_by(Event.id)
                    )
                ).scalars()
            )

    return asyncio.run(read())


def test_mutating_command_writes_one_run_row(db, mail_env, seed_queued) -> None:
    seeded = asyncio.run(seed_queued(lead_status="composed", approved=False))

    result = runner.invoke(cli.app, ["approve", "--lead-id", str(seeded.lead_id)])

    assert result.exit_code == 0, result.output
    rows = _runs()
    assert len(rows) == 1
    assert rows[0].detail["stage"] == "approve"
    assert rows[0].detail["exit_code"] == 0
    assert rows[0].detail["error"] is None


def test_manual_commands_are_not_scheduled_runs(db) -> None:
    result = runner.invoke(cli.app, ["suppress", "--email", "optout@example.com"])

    assert result.exit_code == 0, result.output
    assert _runs() == []


def test_read_only_command_writes_no_run_row(db) -> None:
    result = runner.invoke(cli.app, ["stats"])

    assert result.exit_code == 0, result.output
    assert _runs() == []


def test_failed_command_records_its_exit_code(db, mail_env) -> None:
    result = runner.invoke(cli.app, ["approve", "--lead-id", "999"])

    assert result.exit_code == 2
    rows = _runs()
    assert len(rows) == 1
    assert rows[0].detail["stage"] == "approve"
    assert rows[0].detail["exit_code"] == 2


def test_unconfirmed_live_run_records_exit_two(db, mail_env, seed_queued) -> None:
    asyncio.run(seed_queued())

    result = runner.invoke(cli.app, ["send", "--live"])

    assert result.exit_code == 2
    rows = _runs()
    assert len(rows) == 1
    assert rows[0].detail["stage"] == "send"
    assert rows[0].detail["exit_code"] == 2
