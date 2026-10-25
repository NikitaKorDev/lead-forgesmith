from __future__ import annotations

from pathlib import Path

import aiosqlite

from forgesmith.config import settings
from forgesmith.db.session import get_engine


async def _pragma(connection, name: str):
    return (await connection.exec_driver_sql(f"PRAGMA {name}")).scalar()


async def test_wal_and_busy_timeout_are_applied(db) -> None:
    async with get_engine().connect() as connection:
        journal = await _pragma(connection, "journal_mode")
        timeout = await _pragma(connection, "busy_timeout")

    assert str(journal).lower() == "wal"
    assert int(timeout) == settings.db_busy_timeout_ms


def _sqlite_path() -> Path:
    return Path(settings.database_url.partition("///")[2])


async def test_reader_sees_committed_rows_while_a_writer_is_open(db, seed_queued) -> None:
    await seed_queued()
    path = _sqlite_path()

    writer = await aiosqlite.connect(path)
    reader = await aiosqlite.connect(path)
    try:
        await writer.execute("PRAGMA journal_mode=WAL")
        await writer.execute("BEGIN IMMEDIATE")
        await writer.execute(
            "INSERT INTO suppressions (value, kind, created_at) VALUES ('wal@example.com', 'email', '2026-01-01')"
        )

        async with reader.execute("SELECT COUNT(*) FROM suppressions") as cursor:
            row = await cursor.fetchone()
        assert row is not None and row[0] == 0

        await writer.commit()

        async with reader.execute("SELECT COUNT(*) FROM suppressions") as cursor:
            row = await cursor.fetchone()
        assert row is not None and row[0] == 1
    finally:
        await writer.close()
        await reader.close()
