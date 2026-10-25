from __future__ import annotations

import asyncio

from sqlalchemy import select
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.compose.prompt import FOOTER_VERSION, PROMPT_VERSION
from forgesmith.db.models import Lead, Message, Send
from forgesmith.db.session import session_scope
from forgesmith.mailer.models import LockHeldError, SendStatus
from forgesmith.mailer.runner import send_queued as real_send_queued
from forgesmith.mailer.transport import FakeTransport

runner = CliRunner()


async def _seed(seed_queued, **kwargs):
    return await seed_queued(**kwargs)


def test_approve_moves_a_composed_lead_to_queued(db, mail_env, seed_queued) -> None:
    seeded = asyncio.run(_seed(seed_queued, lead_status="composed", approved=False))

    result = runner.invoke(cli.app, ["approve", "--lead-id", str(seeded.lead_id)])

    assert result.exit_code == 0, result.output

    async def read() -> str:
        async with session_scope() as session:
            lead = await session.get(Lead, seeded.lead_id)
            return lead.status

    assert asyncio.run(read()) == "queued"


def test_approve_refuses_a_superseded_message(db, mail_env, seed_queued) -> None:
    seeded = asyncio.run(_seed(seed_queued, lead_status="composed", approved=False))

    async def add_newer() -> None:
        async with session_scope() as session:
            session.add(
                Message(
                    lead_id=seeded.lead_id,
                    contact_id=seeded.contact_id,
                    subject="Nowszy",
                    body="Treść",
                    prompt_version=PROMPT_VERSION,
                    hooks={"footer_version": FOOTER_VERSION},
                )
            )

    asyncio.run(add_newer())
    result = runner.invoke(
        cli.app,
        ["approve", "--lead-id", str(seeded.lead_id), "--message-id", str(seeded.message_id)],
    )

    assert result.exit_code == 2
    assert "not_latest" in result.output


def test_bulk_approve_requires_yes_when_non_interactive(db, mail_env, seed_queued) -> None:
    asyncio.run(_seed(seed_queued, lead_status="composed", approved=False))

    refused = runner.invoke(cli.app, ["approve", "--all-composed"])
    assert refused.exit_code == 2

    accepted = runner.invoke(cli.app, ["approve", "--all-composed", "--yes"])
    assert accepted.exit_code == 0, accepted.output


def test_dry_send_writes_outbox_and_leaves_lead_queued(db, mail_env, seed_queued) -> None:
    seeded = asyncio.run(_seed(seed_queued))

    result = runner.invoke(cli.app, ["send"])

    assert result.exit_code == 0, result.output
    path = mail_env.outbox_dir / f"lead{seeded.lead_id}-msg{seeded.message_id}.eml"
    assert path.exists()

    async def read():
        async with session_scope() as session:
            lead = await session.get(Lead, seeded.lead_id)
            sends = list((await session.execute(select(Send))).scalars())
        return lead, sends

    lead, sends = asyncio.run(read())
    assert lead is not None and lead.status == "queued"
    assert sends == []


def test_outbox_lists_rendered_files(db, mail_env, seed_queued) -> None:
    asyncio.run(_seed(seed_queued))
    runner.invoke(cli.app, ["send"])

    result = runner.invoke(cli.app, ["outbox"])

    assert result.exit_code == 0
    assert "lead" in result.output


def test_live_send_requires_yes_and_then_sends(db, mail_env, seed_queued, monkeypatch) -> None:
    asyncio.run(_seed(seed_queued))

    refused = runner.invoke(cli.app, ["send", "--live"])
    assert refused.exit_code == 2

    async def with_fake_transport(**kwargs):
        return await real_send_queued(transport=FakeTransport(), **kwargs)

    monkeypatch.setattr(cli, "send_queued", with_fake_transport)
    result = runner.invoke(cli.app, ["send", "--live", "--yes", "--limit", "1"])

    assert result.exit_code == 0, result.output

    async def read():
        async with session_scope() as session:
            return (await session.execute(select(Send))).scalar_one()

    send = asyncio.run(read())
    assert send.status == SendStatus.SENT
    assert send.mode == "live"


def test_send_reports_a_held_lock_as_fatal(db, mail_env, monkeypatch) -> None:
    async def locked(**kwargs):
        raise LockHeldError("data/mailer.lock", 1234)

    monkeypatch.setattr(cli, "send_queued", locked)
    result = runner.invoke(cli.app, ["send", "--live", "--yes"])

    # Phase 5 reserves 75 for "another run holds the lock" so a scheduler can
    # distinguish a busy stage from a real failure.
    assert result.exit_code == 75
    assert "pid 1234" in result.output


def test_mark_send_requires_yes_when_non_interactive(db, seed_queued) -> None:
    seeded = asyncio.run(_seed(seed_queued))

    async def add_pending() -> int:
        async with session_scope() as session:
            send = Send(
                message_id=seeded.message_id,
                to_email=seeded.email,
                status=SendStatus.PENDING,
                mode="live",
            )
            session.add(send)
            await session.flush()
            return send.id

    send_id = asyncio.run(add_pending())
    refused = runner.invoke(cli.app, ["mark", "--send-id", str(send_id), "--status", "sent"])
    assert refused.exit_code == 2

    accepted = runner.invoke(
        cli.app, ["mark", "--send-id", str(send_id), "--status", "sent", "--yes"]
    )
    assert accepted.exit_code == 0, accepted.output


def test_unsuppress_command_reverses_a_suppression(db) -> None:
    runner.invoke(cli.app, ["suppress", "--email", "A@Example.com", "--reason", "opt-out"])

    result = runner.invoke(
        cli.app, ["unsuppress", "--email", "a@example.com", "--reason", "mistake"]
    )

    assert result.exit_code == 0, result.output
    again = runner.invoke(cli.app, ["unsuppress", "--email", "a@example.com"])
    assert again.exit_code == 2
