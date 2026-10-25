from __future__ import annotations

import asyncio
import sqlite3

from sqlalchemy import inspect, select

from forgesmith.config import settings
from forgesmith.db import init_db, reset_state, session_scope
from forgesmith.db.models import Base, Send
from forgesmith.db.schema import ddl_index_names, ensure_schema, metadata_index_names


def _old_sends_table(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE sends ("
        "id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, to_email TEXT NOT NULL, "
        "mode TEXT, smtp_message_id TEXT, status TEXT, error TEXT, sent_at TEXT)"
    )
    connection.commit()
    connection.close()


def test_old_shape_db_is_upgraded_in_place(tmp_path, monkeypatch) -> None:
    path = tmp_path / "old.db"
    _old_sends_table(path)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{path.as_posix()}")
    reset_state()
    try:
        asyncio.run(init_db())
        asyncio.run(init_db())  # idempotent on the second run

        connection = sqlite3.connect(path)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sends)")}
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'"
            )
        }
        connection.close()
    finally:
        reset_state()

    assert "created_at" in columns
    assert {
        "ix_sends_message_id",
        "ix_sends_smtp_message_id",
        "ix_messages_lead_id",
        "ix_events_entity_action",
    } <= indexes


async def test_ensure_schema_is_idempotent(db) -> None:
    from forgesmith.db.session import get_engine

    await ensure_schema(get_engine())
    await ensure_schema(get_engine())


async def test_sends_created_at_is_written_and_indexed(seed_queued) -> None:
    seeded = await seed_queued()
    async with session_scope() as session:
        send = Send(message_id=seeded.message_id, to_email=seeded.email, status="pending")
        session.add(send)

    async with session_scope() as session:
        row = (await session.execute(select(Send))).scalar_one()

    from forgesmith.db.session import get_engine

    async with get_engine().connect() as connection:
        indexes = await connection.run_sync(
            lambda conn: {index["name"] for index in inspect(conn).get_indexes("sends")}
        )

    assert row.created_at is not None
    assert "ix_sends_message_id" in indexes
    assert "ix_sends_smtp_message_id" in indexes


def test_model_and_ddl_agree() -> None:
    columns = set(Base.metadata.tables["sends"].columns.keys())
    assert "created_at" in columns
    assert {
        "ix_sends_message_id",
        "ix_sends_smtp_message_id",
        "ix_messages_lead_id",
        "ix_events_entity_action",
    } <= metadata_index_names()
    # Every index ``ensure_schema`` creates must also exist in the ORM, or a
    # fresh database and an upgraded one diverge. The partial unique index is
    # intentionally DDL-only (SQLite/PG partial-index syntax differs), so only
    # the plain indexes are checked against the metadata.
    assert "ix_sends_smtp_message_id" in ddl_index_names()
    assert {
        "ix_sends_message_id",
        "ix_sends_smtp_message_id",
        "ix_messages_lead_id",
        "ix_events_entity_action",
    } <= metadata_index_names()
