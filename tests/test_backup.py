from __future__ import annotations

import asyncio
import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.config import settings

runner = CliRunner()


def _source_path() -> Path:
    return Path(settings.database_url.partition("///")[2])


def _count(path: Path, table: str) -> int:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else -1
    finally:
        connection.close()


def test_backup_snapshot_matches_the_source(db, seed_queued, tmp_path) -> None:
    asyncio.run(seed_queued(source_id="a.pl", email="biuro@a.pl", domain="a.pl"))
    asyncio.run(seed_queued(source_id="b.pl", email="biuro@b.pl", domain="b.pl"))

    result = runner.invoke(cli.app, ["backup", "--out", str(tmp_path), "--no-outbox"])

    assert result.exit_code == 0, result.output
    snapshots = list(tmp_path.glob("forgesmith-*.db"))
    assert len(snapshots) == 1
    assert _count(snapshots[0], "leads") == _count(_source_path(), "leads") == 2
    assert _count(snapshots[0], "messages") == 2


def test_backup_archives_the_outbox(db, mail_env, seed_queued, tmp_path) -> None:
    asyncio.run(seed_queued())
    runner.invoke(cli.app, ["send"])

    result = runner.invoke(cli.app, ["backup", "--out", str(tmp_path)])

    assert result.exit_code == 0, result.output
    archives = list(tmp_path.glob("outbox-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as tar:
        names = tar.getnames()
    assert any(name.endswith(".eml") for name in names)


def test_backup_refuses_an_existing_target(db, seed_queued, tmp_path, monkeypatch) -> None:
    asyncio.run(seed_queued())

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz or UTC)

    monkeypatch.setattr(cli, "datetime", FixedDateTime)

    first = runner.invoke(cli.app, ["backup", "--out", str(tmp_path), "--no-outbox"])
    second = runner.invoke(cli.app, ["backup", "--out", str(tmp_path), "--no-outbox"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 1
    assert "already exists" in second.output
