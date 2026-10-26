"""``leads.country``: the additive column, the backfill and the observable payload.

The column arrived after the pipeline was already running, so an existing
database must gain it in place and every pre-existing row must be fillable
without touching a row that already knows its country.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from sqlalchemy import inspect, select
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.config import settings
from forgesmith.db import init_db, reset_state, session_scope
from forgesmith.db.models import Lead
from forgesmith.db.repo import LeadRepo
from forgesmith.domain import LeadCandidate, LeadSource

runner = CliRunner()


@pytest.fixture
def sync_db(tmp_path, monkeypatch):
    """A ready database for sync tests that drive ``CliRunner``."""
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    )
    reset_state()
    asyncio.run(init_db())
    yield
    reset_state()


def _legacy_leads_table(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE leads ("
        "id INTEGER PRIMARY KEY, source VARCHAR(32) NOT NULL, "
        "source_id VARCHAR(255) NOT NULL, name VARCHAR(512) NOT NULL, url VARCHAR(2048), "
        "domain VARCHAR(255), city VARCHAR(128), category VARCHAR(255), phone VARCHAR(64), "
        "platform VARCHAR(64), status VARCHAR(32) NOT NULL, raw JSON, created_at DATETIME, "
        "updated_at DATETIME)"
    )
    connection.execute(
        "INSERT INTO leads (source, source_id, name, status, raw) "
        "VALUES ('olx', 'olx:1', 'Mebis', 'new', '{}')"
    )
    connection.commit()
    connection.close()


def test_a_legacy_leads_table_gains_the_country_column(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy.db"
    _legacy_leads_table(path)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{path.as_posix()}")
    reset_state()
    try:
        asyncio.run(init_db())
        asyncio.run(init_db())  # idempotent on the second run

        connection = sqlite3.connect(path)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(leads)")}
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_leads_country'"
            )
        }
        values = list(connection.execute("SELECT country FROM leads"))
        connection.close()
    finally:
        reset_state()

    assert "country" in columns
    assert indexes == {"ix_leads_country"}
    assert values == [(None,)]


def _candidate(domain: str, *, country: str | None = None) -> LeadCandidate:
    return LeadCandidate.from_site(
        source=LeadSource.OLX,
        source_id=f"olx:{domain}",
        name="Sklep",
        site_url=f"https://{domain}/",
        country=country,
    )


async def test_upsert_writes_country_and_refresh_fills_it_in(db) -> None:
    async with session_scope() as session:
        await LeadRepo(session).upsert_many([_candidate("a.pl", country="pl")])
    async with session_scope() as session:
        await LeadRepo(session).upsert_many([_candidate("b.pl")])

    async with session_scope() as session:
        rows = {lead.domain: lead for lead in (await session.execute(select(Lead))).scalars()}
    assert rows["a.pl"].country == "PL"
    assert rows["b.pl"].country is None

    async with session_scope() as session:
        await LeadRepo(session).upsert_many([_candidate("b.pl", country="DE")])
    async with session_scope() as session:
        lead = (await session.execute(select(Lead).where(Lead.domain == "b.pl"))).scalar_one()
    assert lead.country == "DE"


def test_backfill_only_touches_null_rows_and_is_idempotent(sync_db) -> None:
    async def seed() -> None:
        async with session_scope() as session:
            session.add(Lead(source="shopify", source_id="a.pl", name="A", country=None))
            session.add(Lead(source="olx", source_id="olx:2", name="B", country="DE"))

    asyncio.run(seed())

    first = runner.invoke(cli.app, ["db", "backfill-country"])
    second = runner.invoke(cli.app, ["db", "backfill-country"])

    assert first.exit_code == 0, first.output
    assert "1 lead(s) with country PL" in first.output
    assert second.exit_code == 0, second.output
    assert "0 lead(s) with country PL" in second.output

    async def countries() -> dict[str, str | None]:
        async with session_scope() as session:
            rows = (await session.execute(select(Lead))).scalars()
            return {lead.source_id: lead.country for lead in rows}

    assert asyncio.run(countries()) == {"a.pl": "PL", "olx:2": "DE"}


def test_backfill_json_reports_the_count(sync_db) -> None:
    async def seed() -> None:
        async with session_scope() as session:
            session.add(Lead(source="shopify", source_id="a.pl", name="A"))

    asyncio.run(seed())

    result = runner.invoke(cli.app, ["db", "backfill-country", "--default", "de", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload == {"country": "DE", "updated": 1}


def test_backfill_rejects_a_non_country_default(sync_db) -> None:
    result = runner.invoke(cli.app, ["db", "backfill-country", "--default", "Poland"])

    assert result.exit_code == 1
    assert "Invalid country code" in result.output


def test_leads_json_exposes_country(sync_db) -> None:
    async def seed() -> None:
        async with session_scope() as session:
            session.add(
                Lead(source="olx", source_id="olx:1", name="Mebis", country="PL", status="new")
            )

    asyncio.run(seed())

    result = runner.invoke(cli.app, ["leads", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["rows"][0]["country"] == "PL"


async def test_the_index_exists_on_a_fresh_db(db) -> None:
    from forgesmith.db.session import get_engine

    async with get_engine().connect() as connection:
        indexes = await connection.run_sync(
            lambda conn: {index["name"] for index in inspect(conn).get_indexes("leads")}
        )

    assert "ix_leads_country" in indexes
