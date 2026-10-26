"""Idempotent completion of schema pieces ``create_all`` cannot backfill.

``Base.metadata.create_all`` creates missing tables and indexes on a fresh
database, but it never alters an existing table. The additive pieces are
``sends.created_at``, ``leads.country``, the reporting indexes, and the partial
unique index that enforces one live attempt per message, so this module is a
narrow completion step rather than a migration framework.

The SQLite branch is exercised by the default offline suite. The Postgres
branch is only exercised by the optional compose smoke test; both statements are
``IF NOT EXISTS``/introspection-guarded, so running them twice is safe.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from forgesmith.db.models import Base

_CREATED_AT_ALTER = {
    "sqlite": "ALTER TABLE sends ADD COLUMN created_at DATETIME",
    "postgresql": "ALTER TABLE sends ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
}

#: ``leads`` predates the multi-country sources, so every database created
#: before then has no country column. Nullable on purpose: "unknown" is a
#: distinct state from "PL", and ``db backfill-country`` resolves it.
_COUNTRY_ALTER = {
    "sqlite": "ALTER TABLE leads ADD COLUMN country VARCHAR(2)",
    "postgresql": "ALTER TABLE leads ADD COLUMN IF NOT EXISTS country VARCHAR(2)",
}

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_sends_message_id ON sends (message_id)",
    "CREATE INDEX IF NOT EXISTS ix_sends_smtp_message_id ON sends (smtp_message_id)",
    "CREATE INDEX IF NOT EXISTS ix_messages_lead_id ON messages (lead_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_entity_action ON events (entity, entity_id, action)",
    "CREATE INDEX IF NOT EXISTS ix_leads_country ON leads (country)",
)

_INDEX_NAME = re.compile(r"INDEX IF NOT EXISTS (\w+)")

#: Statuses after which a message may never be attempted again. ``failed`` and
#: ``aborted`` are deliberately excluded: both are retryable (``aborted`` never
#: consumed an attempt), while ``pending``/``sent``/``uncertain``/``bounced``
#: all mean "something may have left or is leaving".
_LIVE_STATUSES_SQL = "('pending','sent','uncertain','bounced')"

_UNIQUE_LIVE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sends_message_live "
    "ON sends (message_id) "
    f"WHERE status IN {_LIVE_STATUSES_SQL}"
)

_DUPLICATE_QUERY = (
    "SELECT message_id, COUNT(*) AS n FROM sends "
    f"WHERE status IN {_LIVE_STATUSES_SQL} "
    "GROUP BY message_id HAVING COUNT(*) > 1"
)


def _ensure_live_attempt_index(conn: Connection) -> None:
    """One live attempt per message, enforced by the database itself.

    The Python-side re-check in ``MailRepo.reserve`` can race two processes;
    this partial unique index is what makes the second reservation fail.
    Existing duplicates are reported instead of silently dropped: resolving
    them is an operator decision (``forgesmith sends``).
    """
    duplicates = conn.execute(text(_DUPLICATE_QUERY)).all()
    if duplicates:
        detail = ", ".join(f"message {row[0]} ({row[1]} rows)" for row in duplicates)
        raise RuntimeError(
            "cannot enforce one live attempt per message: duplicate rows exist for "
            f"{detail}. Resolve them with `forgesmith sends` before rerunning."
        )
    conn.execute(text(_UNIQUE_LIVE_INDEX))


def ensure_schema_sync(conn: Connection) -> None:
    """Complete the ledger in place. Safe to run on every ``init_db``."""
    dialect = conn.dialect.name
    inspector = sa.inspect(conn)
    if inspector.has_table("sends"):
        columns = {column["name"] for column in inspector.get_columns("sends")}
        if "created_at" not in columns:
            statement = _CREATED_AT_ALTER.get(dialect)
            if statement is None:
                raise RuntimeError(f"unsupported dialect for send ledger: {dialect}")
            conn.execute(text(statement))

    if inspector.has_table("leads"):
        columns = {column["name"] for column in inspector.get_columns("leads")}
        if "country" not in columns:
            statement = _COUNTRY_ALTER.get(dialect)
            if statement is None:
                raise RuntimeError(f"unsupported dialect for lead country: {dialect}")
            conn.execute(text(statement))

    tables = set(inspector.get_table_names())
    for statement in _INDEXES:
        table = statement.split(" ON ", 1)[1].split(" ", 1)[0]
        if table in tables:
            conn.execute(text(statement))
    if "sends" in tables:
        _ensure_live_attempt_index(conn)


async def ensure_schema(engine: AsyncEngine) -> None:
    """Run :func:`ensure_schema_sync` inside one transaction."""
    async with engine.begin() as conn:
        await conn.run_sync(ensure_schema_sync)


def metadata_index_names() -> set[str]:
    """Index names declared on the ORM metadata (used by the schema test)."""
    return {index.name for table in Base.metadata.tables.values() for index in table.indexes}


def ddl_index_names() -> set[str]:
    """Index names completed by :func:`ensure_schema` on pre-existing tables.

    ``create_all`` never backfills an index on a table that already exists, so
    every index that predates the current model must be listed in ``_INDEXES``
    (or, for the live-attempt guard, ``_UNIQUE_LIVE_INDEX``).
    """
    return {
        match.group(1)
        for statement in (*_INDEXES, _UNIQUE_LIVE_INDEX)
        if (match := _INDEX_NAME.search(statement)) is not None
    }
