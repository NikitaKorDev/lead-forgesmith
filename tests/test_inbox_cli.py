from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.db.models import Lead, Send, Suppression
from forgesmith.db.repo import InboxRepo
from forgesmith.db.session import session_scope
from forgesmith.inbox.models import (
    InboundKind,
    InboxConfigError,
    ParsedInbound,
    RawMessage,
    SyncStats,
)
from forgesmith.mailer.models import LockHeldError

runner = CliRunner()
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


async def _add_send(seeded, *, status: str = "sent") -> int:
    async with session_scope() as session:
        send = Send(
            message_id=seeded.message_id,
            to_email=seeded.email,
            status=status,
            mode="live",
            smtp_message_id="<orig@example.com>",
            created_at=NOW,
        )
        session.add(send)
        await session.flush()
        return send.id


async def _record(parsed: ParsedInbound, uid: int = 1) -> int:
    async with session_scope() as session:
        row = await InboxRepo(session).record_inbound(
            parsed,
            RawMessage("fake/INBOX", uid, 1, b"", None, 10),
            now=NOW,
        )
        assert row is not None
        return row.id


def _dsn(seeded, *, key: str = "a" * 64) -> ParsedInbound:
    return ParsedInbound(
        kind=InboundKind.DSN,
        dedupe_hash=key,
        size=10,
        final_recipient=seeded.email,
        enhanced_status="5.1.1",
        smtp_code=550,
        action="failed",
        reported_message_id="<orig@example.com>",
        detail={"diagnostic": "smtp; 550 5.1.1 rejected"},
    )


def _optout(*, key: str = "b" * 64) -> ParsedInbound:
    return ParsedInbound(
        kind=InboundKind.OPTOUT,
        dedupe_hash=key,
        size=10,
        from_addr="anna@a.pl",
        referenced_ids=("orig@example.com",),
        optout_phrase="prosze nie kontaktowac",
        detail={"optout_phrase": "prosze nie kontaktowac"},
    )


# --------------------------------------------------------------------- sync
def test_inbox_sync_prints_stats(db, mail_env, monkeypatch) -> None:
    async def fake_sync(**kwargs: object) -> SyncStats:
        return SyncStats(scanned=3, ingested=2, applied=1, bounces=1, needs_review=1)

    monkeypatch.setattr(cli, "sync_inbox", fake_sync)
    result = runner.invoke(cli.app, ["inbox", "sync"])

    assert result.exit_code == 0, result.output
    assert "scanned" in result.output
    assert "needs review" in result.output


def test_inbox_sync_passes_dry_run(db, mail_env, monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_sync(**kwargs: object) -> SyncStats:
        seen.update(kwargs)
        return SyncStats(dry_run=2)

    monkeypatch.setattr(cli, "sync_inbox", fake_sync)
    result = runner.invoke(cli.app, ["inbox", "sync", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert seen["dry_run"] is True
    assert "dry run" in result.output


def test_inbox_sync_reports_a_held_lock_as_75(db, mail_env, monkeypatch) -> None:
    async def locked(**kwargs: object) -> SyncStats:
        raise LockHeldError("data/inbox.lock", 4242)

    monkeypatch.setattr(cli, "sync_inbox", locked)
    result = runner.invoke(cli.app, ["inbox", "sync"])

    assert result.exit_code == 75
    assert "4242" in result.output


def test_inbox_sync_reports_a_config_error_as_1(db, mail_env, monkeypatch) -> None:
    async def broken(**kwargs: object) -> SyncStats:
        raise InboxConfigError("IMAP is not configured")

    monkeypatch.setattr(cli, "sync_inbox", broken)
    result = runner.invoke(cli.app, ["inbox", "sync"])

    assert result.exit_code == 1
    assert "IMAP is not configured" in result.output


# --------------------------------------------------------------- list/show
def test_inbox_list_masks_addresses(db, mail_env, seed_queued) -> None:
    asyncio.run(seed_queued())
    asyncio.run(
        _record(
            ParsedInbound(
                kind=InboundKind.REPLY,
                dedupe_hash="c" * 64,
                size=10,
                from_addr="anna@a.pl",
                subject="Re: oferta",
            )
        )
    )

    result = runner.invoke(cli.app, ["inbox", "list"])

    assert result.exit_code == 0, result.output
    assert "a***@a.pl" in result.output
    assert "anna@a.pl" not in result.output
    assert "Inbound (1)" in result.output


def test_inbox_show_missing_id_is_usage_error(db, mail_env) -> None:
    result = runner.invoke(cli.app, ["inbox", "show", "--id", "999"])

    assert result.exit_code == 2
    assert "No inbound message" in result.output


def test_inbox_show_explains_an_optout(db, mail_env, seed_queued) -> None:
    seeded = asyncio.run(seed_queued(lead_status="sent"))
    asyncio.run(_add_send(seeded))
    inbound_id = asyncio.run(_record(_optout()))

    result = runner.invoke(cli.app, ["inbox", "show", "--id", str(inbound_id)])

    assert result.exit_code == 0, result.output
    assert "optout" in result.output.lower()
    assert "--yes" in result.output


# -------------------------------------------------------------------- apply
def test_inbox_apply_one_dsn(db, mail_env, seed_queued) -> None:
    seeded = asyncio.run(seed_queued(lead_status="sent"))
    asyncio.run(_add_send(seeded))
    inbound_id = asyncio.run(_record(_dsn(seeded)))

    result = runner.invoke(cli.app, ["inbox", "apply", "--id", str(inbound_id)])

    assert result.exit_code == 0, result.output
    assert "Applied" in result.output

    async def read() -> tuple[str, list[Suppression]]:
        async with session_scope() as session:
            lead = await session.get(Lead, seeded.lead_id)
            rows = list((await session.execute(select(Suppression))).scalars())
            assert lead is not None
            return lead.status, rows

    status, suppressions = asyncio.run(read())
    assert status == "bounced"
    assert [(row.kind, row.value) for row in suppressions] == [("email", seeded.email)]


def test_inbox_apply_optout_requires_yes(db, mail_env, seed_queued) -> None:
    seeded = asyncio.run(seed_queued(lead_status="sent"))
    asyncio.run(_add_send(seeded))
    inbound_id = asyncio.run(_record(_optout()))

    refused = runner.invoke(cli.app, ["inbox", "apply", "--id", str(inbound_id)])
    assert refused.exit_code == 2
    assert "--yes" in refused.output

    confirmed = runner.invoke(cli.app, ["inbox", "apply", "--id", str(inbound_id), "--yes"])
    assert confirmed.exit_code == 0, confirmed.output

    async def read() -> str:
        async with session_scope() as session:
            lead = await session.get(Lead, seeded.lead_id)
            assert lead is not None
            return lead.status

    assert asyncio.run(read()) == "suppressed"


def test_inbox_apply_all_needs_confirmation(db, mail_env, seed_queued) -> None:
    seeded = asyncio.run(seed_queued(lead_status="sent"))
    asyncio.run(_add_send(seeded))
    asyncio.run(_record(_dsn(seeded, key="d" * 64), uid=1))
    asyncio.run(_record(_dsn(seeded, key="e" * 64), uid=2))

    result = runner.invoke(cli.app, ["inbox", "apply", "--all"])

    assert result.exit_code == 2
    assert "needs --yes" in result.output
