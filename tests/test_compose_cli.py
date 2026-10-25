from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.compose.models import ComposeStats
from forgesmith.db.models import Contact, Lead, Message, Suppression
from forgesmith.db.session import session_scope

runner = CliRunner()


async def _seed_draft() -> int:
    async with session_scope() as session:
        lead = Lead(
            source="shopify",
            source_id="a.pl",
            name="Sklep A",
            domain="a.pl",
            status="composed",
        )
        session.add(lead)
        await session.flush()
        contact = Contact(lead_id=lead.id, email="biuro@a.pl", kind="role", confidence=0.9)
        session.add(contact)
        await session.flush()
        message = Message(
            lead_id=lead.id,
            contact_id=contact.id,
            subject="Pomysł na ofertę",
            body="Dzień dobry,\n\nTreść.\n\nPozdrawiam,\nJan",
            model="qwen2.5:7b",
            prompt_version="phase3-v1",
            hooks={},
        )
        session.add(message)
        await session.flush()
        return message.id


def test_drafts_renders_a_saved_draft(db) -> None:
    asyncio.run(_seed_draft())
    result = runner.invoke(cli.app, ["drafts", "--limit", "5"])

    assert result.exit_code == 0
    assert "Drafts" in result.output


def test_suppress_inserts_a_normalized_suppression(db) -> None:
    result = runner.invoke(
        cli.app,
        ["suppress", "--email", "A@Example.COM", "--reason", "opt-out"],
    )

    assert result.exit_code == 0

    async def read() -> list[Suppression]:
        async with session_scope() as session:
            return list((await session.execute(select(Suppression))).scalars())

    rows = asyncio.run(read())
    assert [(row.value, row.kind, row.reason) for row in rows] == [
        ("a@example.com", "email", "opt-out")
    ]


def test_compose_command_delegates_to_runner(monkeypatch: pytest.MonkeyPatch, db) -> None:
    captured: dict[str, object] = {}

    async def fake_compose(**kwargs: object) -> ComposeStats:
        captured.update(kwargs)
        return ComposeStats(scanned=1, drafted=1, duration_s=0.1)

    monkeypatch.setattr(cli, "compose_pending", fake_compose)
    result = runner.invoke(cli.app, ["compose", "--limit", "2", "--source", "shopify"])

    assert result.exit_code == 0
    assert "scanned" in result.output
    assert captured["limit"] == 2
    assert captured["source"] == "shopify"
