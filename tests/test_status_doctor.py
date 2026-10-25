from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.db.models import Event
from forgesmith.db.session import session_scope

runner = CliRunner()


def _json(result) -> dict:
    start = result.output.index("{")
    return json.loads(result.output[start:])


class _FakeResponse:
    status_code = 200


class _FakeClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        return _FakeResponse()


def test_status_json_surfaces_counts_and_kill_switch(db, mail_env, seed_queued) -> None:
    asyncio.run(seed_queued())

    result = runner.invoke(cli.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["kill_switch"] == "off"
    assert payload["leads"]["total"] == 1
    assert payload["leads"]["by_status"]["queued"] == 1
    assert payload["window"]["open"] in (True, False)
    assert payload["sends"] == {}
    assert payload["uncertain"] == 0
    assert "outbox" in payload


def test_status_marks_the_kill_switch_when_armed(db, mail_env, monkeypatch) -> None:
    monkeypatch.setattr(mail_env, "mail_dry_run", True)

    result = runner.invoke(cli.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    assert _json(result)["kill_switch"] == "armed"


def test_doctor_fails_when_the_database_is_broken(db, mail_env, monkeypatch) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database is unreadable")

    monkeypatch.setattr(cli, "init_db", broken)
    monkeypatch.setattr(cli.httpx, "AsyncClient", _FakeClient)

    result = runner.invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = _json(result)
    assert payload["ok"] is False
    assert any(row["check"] == "database" and row["status"] == "fail" for row in payload["checks"])


def test_doctor_warns_on_a_stale_stage(db, mail_env, monkeypatch) -> None:
    monkeypatch.setattr(mail_env, "stage_stale_after", 1.0)
    monkeypatch.setattr(cli.httpx, "AsyncClient", _FakeClient)

    async def seed_old_run() -> None:
        async with session_scope() as session:
            session.add(
                Event(
                    entity="run",
                    action="finished",
                    detail={"stage": "enrich", "exit_code": 0, "duration_s": 1.0, "error": None},
                    created_at=datetime.now(UTC) - timedelta(days=1),
                )
            )

    asyncio.run(seed_old_run())

    result = runner.invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 3
    payload = _json(result)
    stale = [row for row in payload["checks"] if row["check"] == "run:enrich"]
    assert stale and stale[0]["status"] == "warn"


def test_doctor_json_reports_every_check(db, mail_env, monkeypatch) -> None:
    monkeypatch.setattr(cli.httpx, "AsyncClient", _FakeClient)

    result = runner.invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["ok"] is True
    checks = {row["check"] for row in payload["checks"]}
    assert {"database", "data_dir", "outbox", "backups", "sender", "timezone"} <= checks
    assert "kill_switch" in checks
