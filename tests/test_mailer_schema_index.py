from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from forgesmith.compose.prompt import FOOTER_VERSION
from forgesmith.db import MailRepo, ensure_schema, session_scope
from forgesmith.db.models import Send
from forgesmith.db.session import get_engine
from forgesmith.mailer.models import RecheckReason, RecheckResult


async def _index_sql() -> dict[str, str]:
    async with get_engine().connect() as connection:
        rows = (
            await connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='sends'"
            )
        ).all()
    return {name: sql or "" for name, sql in rows}


async def test_partial_unique_index_exists_on_a_fresh_db(db) -> None:
    indexes = await _index_sql()

    assert "uq_sends_message_live" in indexes
    assert "WHERE status IN" in indexes["uq_sends_message_live"]


async def test_second_live_attempt_is_refused_by_the_database(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued()

    async with session_scope() as session:
        session.add(
            Send(
                message_id=seeded.message_id,
                to_email=seeded.email,
                status="pending",
                mode="live",
            )
        )

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(
                Send(
                    message_id=seeded.message_id,
                    to_email=seeded.email,
                    status="sent",
                    mode="live",
                )
            )


async def test_failed_and_aborted_stay_retryable(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued()

    async with session_scope() as session:
        for status in ("failed", "aborted"):
            session.add(
                Send(
                    message_id=seeded.message_id,
                    to_email=seeded.email,
                    status=status,
                    mode="live",
                )
            )
        session.add(
            Send(
                message_id=seeded.message_id,
                to_email=seeded.email,
                status="pending",
                mode="live",
            )
        )


async def test_duplicate_rows_abort_schema_upgrade_with_ids(db, mail_env, seed_queued) -> None:
    seeded = await seed_queued()
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP INDEX IF EXISTS uq_sends_message_live")

    async with session_scope() as session:
        for _ in range(2):
            session.add(
                Send(
                    message_id=seeded.message_id,
                    to_email=seeded.email,
                    status="pending",
                    mode="live",
                )
            )

    with pytest.raises(RuntimeError, match=f"message {seeded.message_id}"):
        await ensure_schema(engine)


async def test_reserve_maps_an_index_race_to_existing_attempt(
    db, mail_env, seed_queued, monkeypatch
) -> None:
    seeded = await seed_queued()
    async with session_scope() as session:
        snapshot = (await MailRepo(session).select_queued())[0]
        session.add(
            Send(
                message_id=seeded.message_id,
                to_email=seeded.email,
                status="pending",
                mode="live",
            )
        )

    async def always_ok(self, *args, **kwargs):
        return RecheckResult()

    monkeypatch.setattr(MailRepo, "recheck", always_ok)

    async with session_scope() as session:
        result = await MailRepo(session).reserve(
            snapshot,
            smtp_message_id="<race@example.com>",
            now=datetime.now(UTC),
            expected_footer_version=FOOTER_VERSION,
            max_attempts=3,
        )

    assert isinstance(result, RecheckResult)
    assert result.reason == RecheckReason.EXISTING_ATTEMPT
    assert result.detail["guard"] == "db_unique_index"
