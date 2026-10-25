"""CLI entry point: ``forgesmith <group> <command>``."""

from __future__ import annotations

import asyncio
import functools
import json as jsonlib
import os
import shutil
import signal
import socket
import sqlite3
import sys
import tarfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, TypeVar
from urllib.parse import urlparse

import httpx
import structlog
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import text

from forgesmith import __version__
from forgesmith.browser import BrowserFetcher
from forgesmith.compose import ComposeConfigError, ComposeStats, compose_pending
from forgesmith.compose.prompt import FOOTER_VERSION, PROMPT_VERSION, SenderProfile
from forgesmith.config import settings
from forgesmith.console import force_utf8_stdio
from forgesmith.db import (
    ComposeRepo,
    EnrichmentRepo,
    InboxRepo,
    LeadRepo,
    MailRepo,
    init_db,
    session_scope,
)
from forgesmith.db.models import Event
from forgesmith.domain import LeadCandidate, UpsertStats
from forgesmith.eligibility import LeadState, reason_for_skip
from forgesmith.enrichment import RunStats, enrich_pending
from forgesmith.http import PoliteClient
from forgesmith.inbox.models import InboundKind, InboundStatus, InboxConfigError, InboxError
from forgesmith.inbox.runner import sync_inbox
from forgesmith.logs import configure_logging
from forgesmith.mailer.models import (
    ApprovalRefused,
    LockHeldError,
    QueuedMessage,
    SendConfigError,
    SendRefused,
    SendStats,
    TransportError,
)
from forgesmith.mailer.policy import MailPolicy, ensure_utc, mask_email
from forgesmith.mailer.runner import send_queued
from forgesmith.mailer.transport import SmtpTransport
from forgesmith.sources import BaseSource, OlxSource, ShopifySource, seeds_from_paths

app = typer.Typer(add_completion=False, help="Asynchronous B2B client acquisition pipeline.")
source_app = typer.Typer(help="Discover leads from a marketplace.")
db_app = typer.Typer(help="Database lifecycle.")
app.add_typer(source_app, name="source")
app.add_typer(db_app, name="db")

console = Console()
log = structlog.get_logger("forgesmith.cli")

BATCH_SIZE = 50
SourceFactory = Callable[[object], BaseSource]
T = TypeVar("T")

#: SIGTERM/ctrl-C only takes effect *between* messages (never inside
#: ``transport.deliver``), so a container stop cannot turn a send uncertain.
_STOP = False


def _request_stop(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def _stop_requested() -> bool:
    return _STOP


@app.callback()
def bootstrap(
    log_level: Annotated[
        str, typer.Option("--log-level", help="debug | info | warning | error")
    ] = "info",
) -> None:
    """Global options."""
    force_utf8_stdio()
    configure_logging(log_level, json=settings.log_json)


# ------------------------------------------------------------------ run ledger
async def _record_run(
    stage: str,
    *,
    exit_code: int,
    duration_s: float,
    error: str | None,
    stop_reason: str | None = None,
) -> None:
    await init_db()
    async with session_scope() as session:
        session.add(
            Event(
                entity="run",
                action="finished",
                detail={
                    "stage": stage,
                    "exit_code": exit_code,
                    "duration_s": duration_s,
                    "error": error,
                    "stop_reason": stop_reason,
                },
            )
        )


@asynccontextmanager
async def _run_ledger(stage: str) -> AsyncIterator[dict[str, object]]:
    """Record every scheduled stage so ``status``/``doctor`` see what ran last."""
    started = time.monotonic()
    meta: dict[str, object] = {}
    exit_code = 0
    error: str | None = None
    try:
        yield meta
    except typer.Exit as exc:
        exit_code = int(exc.exit_code or 0)
        raise
    except LockHeldError:
        exit_code = 75
        raise
    except SendRefused:
        exit_code = 2
        raise
    except SendConfigError:
        exit_code = 1
        raise
    except BaseException as exc:
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"[:200]
        raise
    finally:
        try:
            await _record_run(
                stage,
                exit_code=exit_code,
                duration_s=round(time.monotonic() - started, 2),
                error=error,
                stop_reason=str(meta["stop_reason"]) if meta.get("stop_reason") else None,
            )
        except Exception as exc:  # a ledger failure must never mask the real error
            log.warning("run.ledger_failed", stage=stage, error=str(exc))


def _ledger(stage: str) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorate(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: object, **kwargs: object) -> T:
            async with _run_ledger(stage) as meta:
                result = await fn(*args, **kwargs)
                stop_reason = getattr(result, "stop_reason", None)
                if stop_reason:
                    meta["stop_reason"] = stop_reason
                return result

        return wrapper

    return decorate


async def _consume_logged(factory: SourceFactory, label: str, engine: str = "http") -> UpsertStats:
    """``_consume`` plus a run-ledger row for the stage."""
    async with _run_ledger(label):
        return await _consume(factory, label, engine)


async def _consume(factory: SourceFactory, label: str, engine: str = "http") -> UpsertStats:
    await init_db()
    totals = UpsertStats()

    fetcher = BrowserFetcher() if engine == "browser" else PoliteClient()
    async with fetcher as client:
        source = factory(client)
        batch: list[LeadCandidate] = []

        async def flush() -> UpsertStats:
            if not batch:
                return UpsertStats()
            async with session_scope() as session:
                stats = await LeadRepo(session).upsert_many(batch)
            batch.clear()
            return stats

        with console.status(f"[cyan]Collecting from {label}...") as status:
            async for candidate in source.discover():
                batch.append(candidate)
                if len(batch) >= BATCH_SIZE:
                    totals += await flush()
                    status.update(
                        f"[cyan]{label}: {totals.inserted + totals.updated} stored, "
                        f"{totals.duplicates} dupes"
                    )
            totals += await flush()

    console.print(
        f"[green]{label}[/]: {totals.inserted} new, {totals.updated} refreshed, "
        f"{totals.duplicates} duplicate(s) skipped"
    )
    return totals


# --------------------------------------------------------------------- db
@db_app.command("init")
def db_init() -> None:
    """Create tables if they do not exist."""

    async def main() -> None:
        await init_db()

    asyncio.run(main())
    console.print(f"[green]Database ready[/]: {settings.database_url}")


# ----------------------------------------------------------------- sources
@source_app.command("shopify")
def source_shopify(
    seeds: Annotated[
        list[Path] | None,
        typer.Option("--seeds", help="Seed files with one store domain per line."),
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Max leads to store.")] = None,
) -> None:
    """Probe Shopify storefronts and store their catalogue metadata."""
    paths = seeds or [settings.seeds_file]
    try:
        seed_list = seeds_from_paths(paths)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc

    if not seed_list:
        console.print("[yellow]No seeds found.[/]")
        raise typer.Exit(code=1)

    console.print(f"Probing {len(seed_list)} seed(s)...")
    asyncio.run(
        _consume_logged(
            lambda client: ShopifySource(client, seeds=seed_list, limit=limit), "shopify"
        )
    )


@source_app.command("olx")
def source_olx(
    query: Annotated[
        list[str],
        typer.Option("--query", "-q", help="Search phrase, e.g. 'meble dębowe'."),
    ] = ["meble"],
    category: Annotated[
        list[str] | None,
        typer.Option("--category", help="OLX category path, e.g. nieruchomosci/mieszkania/wynajem"),
    ] = None,
    city: Annotated[
        list[str],
        typer.Option("--city", help="OLX city slug, e.g. warszawa"),
    ] = [],
    pages: Annotated[int, typer.Option(help="Listing pages per query/city.")] = 2,
    limit: Annotated[int | None, typer.Option(help="Max leads to store.")] = None,
    engine: Annotated[
        str,
        typer.Option(help="browser (default; OLX blocks plain HTTP) or http."),
    ] = "browser",
) -> None:
    """Crawl OLX listing pages into leads."""
    asyncio.run(
        _consume_logged(
            lambda client: OlxSource(
                client,
                queries=query,
                categories=category or [],
                cities=city,
                max_pages=pages,
                limit=limit,
            ),
            "olx",
            engine,
        )
    )


# ------------------------------------------------------------------- reports
@app.command("leads")
def leads(
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 25,
    status: Annotated[str | None, typer.Option(help="Filter by status.")] = None,
    source: Annotated[str | None, typer.Option(help="Filter by source.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List stored leads."""

    async def main() -> tuple[list[dict[str, object]], int]:
        await init_db()
        async with session_scope() as session:
            repo = LeadRepo(session)
            rows = await repo.list(limit=limit, status=status, source=source)
            total = await repo.count()
            eligibility = await repo.eligibility_map(
                lead_ids=[lead.id for lead in rows],
                compose_max_attempts=settings.compose_max_attempts,
                enrich_max_attempts=settings.enrich_max_attempts,
            )
            payload = []
            for lead in rows:
                next_step = eligibility.get(lead.id)
                payload.append(
                    {
                        "id": lead.id,
                        "source": lead.source,
                        "name": lead.name,
                        "domain": lead.domain,
                        "city": lead.city,
                        "category": lead.category,
                        "status": lead.status,
                        "platform": lead.platform,
                        "phone": lead.phone,
                        "url": lead.url,
                        "state": str(next_step.state) if next_step else None,
                        "next_action": next_step.action if next_step else None,
                        "action_label": next_step.label if next_step else None,
                        "reason": next_step.reason if next_step else None,
                    }
                )
        return payload, total

    rows, total = asyncio.run(main())
    if json_output:
        print(
            jsonlib.dumps(
                {"total": total, "count": len(rows), "rows": rows},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return

    table = Table(title=f"Leads (showing {len(rows)} of {total})")
    for column in ("id", "source", "name", "domain", "city", "category", "status"):
        table.add_column(column, overflow="fold")
    for lead in rows:
        table.add_row(
            str(lead["id"]),
            str(lead["source"]),
            str(lead["name"]),
            str(lead["domain"] or "-"),
            str(lead["city"] or "-"),
            str(lead["category"] or "-"),
            str(lead["status"]),
        )
    console.print(table)


@app.command("stats")
def stats() -> None:
    """Show lead counts by source and status."""

    async def main() -> None:
        await init_db()
        async with session_scope() as session:
            repo = LeadRepo(session)
            by_status = await repo.status_counts()
            by_source = await repo.source_counts()
            total = await repo.count()

        table = Table(title=f"Leads: {total}")
        table.add_column("bucket")
        table.add_column("count", justify="right")
        for name, count in sorted(by_status.items(), key=lambda item: -item[1]):
            table.add_row(f"status: {name}", str(count))
        for name, count in sorted(by_source.items(), key=lambda item: -item[1]):
            table.add_row(f"source: {name}", str(count))
        console.print(table)

    asyncio.run(main())


# ---------------------------------------------------------------- enrichment
@app.command("enrich")
def enrich(
    limit: Annotated[int | None, typer.Option(help="Max leads to process.")] = None,
    source: Annotated[str | None, typer.Option(help="Filter by source, e.g. shopify.")] = None,
    lead_id: Annotated[int | None, typer.Option(help="Process a single lead id.")] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-enrich new/enriched/skipped leads; never sent ones."),
    ] = False,
    engine: Annotated[
        str,
        typer.Option(help="auto (HTTP first, browser fallback), http, or browser."),
    ] = "auto",
    resolve_only: Annotated[
        bool,
        typer.Option("--resolve-only", help="Resolve domains only; do not crawl."),
    ] = False,
    deep_resolve: Annotated[
        bool,
        typer.Option("--deep-resolve", help="Try OLX offer pages when no shop subdomain exists."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Resolve storefronts, extract company emails and context."""
    if engine not in {"auto", "http", "browser"}:
        console.print(f"[red]Unknown engine: {engine}[/] (use auto, http, or browser)")
        raise typer.Exit(code=1)

    async def _run() -> RunStats:
        async with _run_ledger("enrich"):
            return await enrich_pending(
                limit=limit,
                source=source,
                lead_id=lead_id,
                force=force,
                resolve_only=resolve_only,
                deep_resolve=deep_resolve,
                engine=engine,
            )

    stats = asyncio.run(_run())
    if json_output:
        print(jsonlib.dumps(asdict(stats), ensure_ascii=False, indent=2, default=str))
        return
    _print_run_stats(stats, resolve_only=resolve_only)


def _print_run_stats(stats: RunStats, *, resolve_only: bool) -> None:
    table = Table(title="Enrichment summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    rows = [
        ("scanned", stats.scanned),
        ("resolved", stats.resolved),
        ("overridden", stats.overridden),
        ("enriched", stats.enriched),
        ("skipped", stats.skipped),
        ("emails stored", stats.emails),
        ("no site", stats.no_site),
        ("no contact", stats.no_contact),
        ("blocked", stats.blocked),
        ("failed", stats.failed),
        ("duplicates", stats.duplicates),
        ("duration (s)", stats.duration_s),
    ]
    if resolve_only:
        rows = [
            ("scanned", stats.scanned),
            ("resolved", stats.resolved),
            ("overridden", stats.overridden),
            ("unresolved", stats.no_site),
            ("duplicates", stats.duplicates),
            ("duration (s)", stats.duration_s),
        ]
    for name, value in rows:
        table.add_row(name, str(value))
    console.print(table)


@app.command("contacts")
def contacts(
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 50,
    lead_id: Annotated[int | None, typer.Option(help="Filter by lead id.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show observed contacts that Phase 3 will use."""

    async def main() -> list[dict[str, object]]:
        await init_db()
        async with session_scope() as session:
            rows = await EnrichmentRepo(session).list_contacts(limit=limit, lead_id=lead_id)
            return [
                {
                    "lead_id": lead.id,
                    "lead_name": lead.name,
                    "domain": lead.domain,
                    "email": contact.email,
                    "kind": contact.kind,
                    "confidence": contact.confidence,
                    "source_url": contact.source_url,
                }
                for contact, lead in rows
            ]

    rows = asyncio.run(main())
    if json_output:
        print(
            jsonlib.dumps(
                {"count": len(rows), "rows": rows},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return

    table = Table(title=f"Contacts ({len(rows)})")
    for column in ("lead", "name", "email", "kind", "confidence", "source"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(
            str(row["lead_id"]),
            str(row["lead_name"]),
            str(row["email"]),
            str(row["kind"]),
            f"{float(row['confidence'] or 0):.2f}",
            str(row["source_url"] or "-"),
        )
    console.print(table)


# -------------------------------------------------------------------- compose
@app.command("compose")
def compose(
    limit: Annotated[int | None, typer.Option(help="Max leads to process.")] = None,
    source: Annotated[str | None, typer.Option(help="Filter by source, e.g. shopify.")] = None,
    lead_id: Annotated[int | None, typer.Option(help="Process a single lead id.")] = None,
    regenerate: Annotated[
        bool,
        typer.Option("--regenerate", help="Create a new draft even when one already exists."),
    ] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Retry leads that reached the failed-attempt cap."),
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Report what would be drafted; no Ollama call and no writes.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Generate grounded Polish drafts for enriched leads."""
    if check:
        report = asyncio.run(
            _compose_check(
                limit=limit,
                source=source,
                lead_id=lead_id,
                regenerate=regenerate,
                retry_failed=retry_failed,
            )
        )
        if json_output:
            print(jsonlib.dumps(report, ensure_ascii=False, indent=2, default=str))
            return
        _print_compose_check(report)
        return

    try:

        async def _run() -> ComposeStats:
            async with _run_ledger("compose"):
                return await compose_pending(
                    limit=limit,
                    source=source,
                    lead_id=lead_id,
                    regenerate=regenerate,
                    retry_failed=retry_failed,
                )

        stats = asyncio.run(_run())
    except ComposeConfigError as exc:
        console.print(f"[red]Compose is not configured:[/] {exc}")
        raise typer.Exit(code=1) from exc

    note: str | None = None
    if stats.scanned == 0:
        # A scheduled compose on a quiet night must not look like a failure, so
        # the exit code stays 0 -- but "scanned 0" on its own is useless. Say
        # exactly which rule rejected the lead(s).
        note = asyncio.run(
            _compose_no_op_note(
                lead_id=lead_id,
                source=source,
                regenerate=regenerate,
                retry_failed=retry_failed,
            )
        )

    if json_output:
        payload = {
            "scanned": stats.scanned,
            "skipped": stats.skipped,
            "drafted": stats.drafted,
            "failed": stats.failed,
            "duration_s": stats.duration_s,
            "reason": note,
            "outcomes": [
                {"lead_id": item.lead_id, "outcome": item.outcome, "reason": item.reason}
                for item in stats.outcomes
            ],
        }
        print(jsonlib.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    _print_compose_stats(stats)
    if note:
        console.print(f"[yellow]Nothing to draft[/] — {note}")


async def _compose_check(
    *,
    limit: int | None,
    source: str | None,
    lead_id: int | None,
    regenerate: bool,
    retry_failed: bool,
) -> dict[str, object]:
    """Eligibility report shared by the GUI's Draft buttons and ``--check``."""
    await init_db()
    async with session_scope() as session:
        repo = LeadRepo(session)
        eligible = await repo.count_composable(
            source=source,
            lead_id=lead_id,
            regenerate=regenerate,
            retry_failed=retry_failed,
            max_attempts=settings.compose_max_attempts,
        )
        if lead_id is not None:
            checked_ids = [lead_id]
        else:
            checked_ids = [
                lead.id
                for lead in await repo.select_composable(
                    limit=limit,
                    source=source,
                    regenerate=regenerate,
                    retry_failed=retry_failed,
                    max_attempts=settings.compose_max_attempts,
                )
            ]
        classification = await repo.eligibility_map(
            lead_ids=checked_ids,
            compose_max_attempts=settings.compose_max_attempts,
            enrich_max_attempts=settings.enrich_max_attempts,
            retry_failed=retry_failed,
        )
    rows = []
    for candidate in sorted(checked_ids):
        item = classification.get(candidate)
        rows.append(
            {
                "lead_id": candidate,
                "state": str(item.state) if item else "missing",
                "next_action": item.action if item else None,
                "reason": item.reason if item else "lead does not exist",
                "eligible": candidate in classification
                and (
                    item.state == LeadState.READY_TO_DRAFT
                    or (regenerate and item.state == LeadState.DRAFTED)
                ),
            }
        )
    return {
        "eligible": eligible,
        "checked": len(checked_ids),
        "lead_id": lead_id,
        "regenerate": regenerate,
        "leads": rows,
    }


def _print_compose_check(report: dict[str, object]) -> None:
    rows = report.get("leads") or []
    assert isinstance(rows, list)
    eligible = int(report["eligible"] or 0)
    lead_id = report.get("lead_id")
    if lead_id is not None:
        row = rows[0] if rows else {}
        assert isinstance(row, dict)
        verdict = "eligible" if row.get("eligible") else "not eligible"
        console.print(
            f"[{'green' if row.get('eligible') else 'yellow'}]{verdict}[/] "
            f"lead {lead_id} ({row.get('state')}): {row.get('reason')}"
        )
        return
    table = Table(title=f"Compose check: {eligible} eligible, {len(rows)} checked")
    for column in ("lead", "state", "next", "reason"):
        table.add_column(column, overflow="fold")
    for row in rows:
        assert isinstance(row, dict)
        table.add_row(
            str(row["lead_id"]),
            str(row["state"]),
            str(row["next_action"] or "-"),
            str(row["reason"] or "-"),
        )
    console.print(table)


async def _compose_no_op_note(
    *,
    lead_id: int | None,
    source: str | None,
    regenerate: bool,
    retry_failed: bool,
) -> str:
    """Explain why a compose run selected nothing."""
    await init_db()
    async with session_scope() as session:
        repo = LeadRepo(session)
        if lead_id is not None:
            classification = await repo.eligibility_map(
                lead_ids=[lead_id],
                compose_max_attempts=settings.compose_max_attempts,
                enrich_max_attempts=settings.enrich_max_attempts,
                retry_failed=retry_failed,
            )
            return reason_for_skip(
                lead_id=lead_id,
                found=lead_id in classification,
                eligibility=classification.get(lead_id),
                regenerate=regenerate,
            )
        classification = await repo.eligibility_map(
            compose_max_attempts=settings.compose_max_attempts,
            enrich_max_attempts=settings.enrich_max_attempts,
            retry_failed=retry_failed,
            source=source,
        )
    counts: dict[str, int] = {}
    for item in classification.values():
        counts[str(item.state)] = counts.get(str(item.state), 0) + 1
    if not counts:
        return "no leads are stored yet — run `forgesmith source` first"
    scope = f" for source {source}" if source else ""
    groups = (
        ("not enriched yet", (LeadState.COLLECTED, LeadState.READY_TO_ENRICH)),
        ("already drafted", (LeadState.DRAFTED,)),
        ("already approved", (LeadState.APPROVED,)),
        ("parked", (LeadState.PARKED,)),
        ("finished", (LeadState.DONE,)),
        ("ready to draft", (LeadState.READY_TO_DRAFT,)),
    )
    parts = []
    for label, states in groups:
        count = sum(counts.get(str(state), 0) for state in states)
        if count:
            parts.append(f"{count} {label}")
    return f"no lead is ready to draft{scope}: " + ", ".join(parts)


def _print_compose_stats(stats: ComposeStats) -> None:
    table = Table(title="Compose summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for name, value in (
        ("scanned", stats.scanned),
        ("skipped", stats.skipped),
        ("drafted", stats.drafted),
        ("failed", stats.failed),
        ("duration (s)", stats.duration_s),
    ):
        table.add_row(name, str(value))
    console.print(table)


@app.command("drafts")
def drafts(
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 20,
    lead_id: Annotated[int | None, typer.Option(help="Filter by lead id.")] = None,
    message_id: Annotated[int | None, typer.Option(help="Show one exact message id.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show generated drafts for human review."""

    async def main() -> list[dict[str, object]]:
        await init_db()
        async with session_scope() as session:
            rows = await ComposeRepo(session).latest_messages(
                limit=limit,
                lead_id=lead_id,
                message_id=message_id,
            )
            return [
                {
                    "message_id": message.id,
                    "lead_id": lead.id,
                    "company": lead.domain or lead.name,
                    "lead_status": lead.status,
                    "contact": contact.email if contact else None,
                    "subject": message.subject,
                    "body": message.body,
                    "model": message.model,
                    "prompt_version": message.prompt_version,
                    "footer_version": (message.hooks or {}).get("footer_version"),
                    "created_at": message.created_at,
                }
                for message, lead, contact in rows
            ]

    rows = asyncio.run(main())
    if json_output:
        print(
            jsonlib.dumps(
                {"count": len(rows), "rows": rows},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return

    table = Table(title=f"Drafts ({len(rows)})")
    for column in (
        "msg",
        "lead",
        "company/domain",
        "contact",
        "subject",
        "body preview",
        "model",
        "prompt",
        "created",
    ):
        table.add_column(column, overflow="fold")
    for row in rows:
        preview = " ".join(str(row["body"]).split())[:160]
        created = (
            row["created_at"].strftime("%Y-%m-%d %H:%M")
            if isinstance(row["created_at"], datetime)
            else "-"
        )
        table.add_row(
            str(row["message_id"]),
            str(row["lead_id"]),
            str(row["company"]),
            str(row["contact"] or "-"),
            str(row["subject"]),
            preview,
            str(row["model"] or "-"),
            str(row["prompt_version"] or "-"),
            created,
        )
    console.print(table)


@app.command("suppress")
def suppress(
    email: Annotated[str | None, typer.Option("--email", help="Suppress one address.")] = None,
    domain: Annotated[str | None, typer.Option("--domain", help="Suppress one domain.")] = None,
    reason: Annotated[str | None, typer.Option(help="Why the opt-out was recorded.")] = None,
) -> None:
    """Record an email or domain opt-out before Phase 4 exists."""
    if bool(email) == bool(domain):
        console.print("[red]Pass exactly one of --email or --domain.[/]")
        raise typer.Exit(code=1)

    value = (email or domain or "").strip().lower()
    kind = "email" if email else "domain"

    async def main() -> None:
        await init_db()
        async with session_scope() as session:
            row = await ComposeRepo(session).create_suppression(
                value=value,
                kind=kind,
                reason=reason,
            )
        console.print(f"[green]Suppressed[/] {row.kind}: {row.value}")

    asyncio.run(main())


@app.command("version")
def version() -> None:
    """Print the version."""
    console.print(f"forgesmith {__version__}")


# --------------------------------------------------------------- phase 4 mail
def _require_sender() -> SenderProfile:
    try:
        return SenderProfile.from_settings(settings)
    except ComposeConfigError as exc:
        console.print(f"[red]Mailer is not configured:[/] {exc}")
        raise typer.Exit(code=1) from exc


def _confirm_bulk(count: int, yes: bool, action: str) -> None:
    """Bulk actions need ``--yes`` when non-interactive or larger than one item."""
    if count == 0:
        return
    if count > 1 or not sys.stdin.isatty():
        if not yes:
            console.print(f"[red]{action} needs --yes[/] ({count} item(s), non-interactive stdin)")
            raise typer.Exit(code=2)


@app.command("approve")
def approve(
    lead_id: Annotated[int | None, typer.Option("--lead-id", help="Approve one lead.")] = None,
    all_composed: Annotated[
        bool,
        typer.Option("--all-composed", help="Approve every composed lead (bulk)."),
    ] = False,
    message_id: Annotated[
        int | None,
        typer.Option("--message-id", help="Assert this is the latest message id."),
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Max leads in a bulk run.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the bulk confirmation.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Approve the latest draft: composed -> queued."""
    if settings.approval_channel.strip().lower() == "unattended":
        console.print(
            "[red]Approval is human-only[/]: FORGESMITH_APPROVAL_CHANNEL=unattended "
            "is set; run approve from an interactive shell without it."
        )
        raise typer.Exit(code=2)
    if bool(lead_id) == bool(all_composed):
        console.print("[red]Pass exactly one of --lead-id or --all-composed.[/]")
        raise typer.Exit(code=2)
    if all_composed and message_id is not None:
        console.print("[red]--message-id only applies to a single --lead-id.[/]")
        raise typer.Exit(code=2)

    _require_sender()

    @_ledger("approve")
    async def run() -> None:
        await init_db()
        if not all_composed:
            assert lead_id is not None
            async with session_scope() as session:
                repo = MailRepo(session)
                try:
                    message, already = await repo.approve(
                        lead_id,
                        message_id=message_id,
                        expected_footer_version=FOOTER_VERSION,
                        expected_prompt_version=PROMPT_VERSION,
                    )
                except ApprovalRefused as exc:
                    console.print(f"[red]Refused[/] lead {lead_id}: {exc.reason}")
                    raise typer.Exit(code=2) from exc
            state = "already approved" if already else "approved"
            if json_output:
                print(
                    jsonlib.dumps(
                        {
                            "lead_id": lead_id,
                            "message_id": message.id,
                            "approved": not already,
                            "already": already,
                            "approved_count": 0 if already else 1,
                            "refused": 0,
                            "skipped": 0,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            console.print(f"[green]{state.capitalize()}[/] lead {lead_id}, message {message.id}")
            return

        async with session_scope() as session:
            repo = MailRepo(session)
            ids = await repo.composed_lead_ids(limit=limit)
            preview = []
            for candidate in ids:
                message = await repo.latest_message(candidate)
                if message is not None:
                    preview.append((candidate, message))

        table = Table(title=f"Approving {len(ids)} lead(s)")
        for column in ("lead", "msg", "subject", "created"):
            table.add_column(column, overflow="fold")
        for candidate, message in preview:
            created = message.created_at.strftime("%Y-%m-%d %H:%M") if message.created_at else "-"
            table.add_row(str(candidate), str(message.id), message.subject, created)
        if not json_output:
            console.print(table)
        _confirm_bulk(len(ids), yes, "Bulk approve")

        approved = already = refused = 0
        refused_leads: list[dict[str, object]] = []
        for candidate, message in preview:
            async with session_scope() as session:
                try:
                    _, was_already = await MailRepo(session).approve(
                        candidate,
                        message_id=message.id,
                        expected_footer_version=FOOTER_VERSION,
                        expected_prompt_version=PROMPT_VERSION,
                        bulk=True,
                    )
                except ApprovalRefused as exc:
                    refused += 1
                    refused_leads.append({"lead_id": candidate, "reason": exc.reason})
                    if not json_output:
                        console.print(f"[yellow]refused[/] lead {candidate}: {exc.reason}")
                    continue
            if was_already:
                already += 1
            else:
                approved += 1
        skipped = max(0, len(ids) - len(preview))
        if json_output:
            print(
                jsonlib.dumps(
                    {
                        "lead_id": None,
                        "approved_count": approved,
                        "already": already,
                        "refused": refused,
                        "skipped": skipped,
                        "refused_leads": refused_leads,
                        "preview": [
                            {
                                "lead_id": candidate,
                                "message_id": message.id,
                                "subject": message.subject,
                            }
                            for candidate, message in preview
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        console.print(
            f"[green]approved {approved}[/], already {already}, "
            f"refused {refused}, skipped {skipped}"
        )

    asyncio.run(run())


@app.command("unapprove")
def unapprove(
    lead_id: Annotated[int, typer.Option("--lead-id", help="Retract one lead's approval.")],
) -> None:
    """Retract approval: queued -> composed (only without live sends)."""

    @_ledger("unapprove")
    async def run() -> None:
        await init_db()
        async with session_scope() as session:
            try:
                await MailRepo(session).unapprove(lead_id)
            except ApprovalRefused as exc:
                console.print(f"[red]Refused[/] lead {lead_id}: {exc.reason}")
                raise typer.Exit(code=2) from exc
        console.print(f"[green]Unapproved[/] lead {lead_id}; status is composed again")

    asyncio.run(run())


@app.command("send")
def send(
    limit: Annotated[int | None, typer.Option(help="Max messages to process.")] = None,
    lead_id: Annotated[int | None, typer.Option(help="Send only this lead.")] = None,
    source: Annotated[str | None, typer.Option(help="Filter by source.")] = None,
    live: Annotated[
        bool,
        typer.Option("--live", help="Actually send over SMTP (default: dry-run .eml)."),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm a live run non-interactively.")
    ] = False,
    no_delay: Annotated[
        bool, typer.Option("--no-delay", help="Skip randomized delays between sends.")
    ] = False,
    force_window: Annotated[
        bool, typer.Option("--force-window", help="Ignore the sending window (not the cap).")
    ] = False,
    max_runtime: Annotated[
        float | None,
        typer.Option(
            "--max-runtime",
            help="Wall-clock budget in seconds (0/default: FORGESMITH_SEND_MAX_RUNTIME).",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Render dry-run .eml files, or deliver approved messages with --live."""
    _require_sender()

    def confirm(snapshots: Sequence[QueuedMessage]) -> bool:
        console.print(
            f"[bold red]LIVE RUN[/] {len(snapshots)} message(s); the first recipients are:"
        )
        for snapshot in snapshots[:5]:
            console.print(
                f"  lead {snapshot.lead_id} msg {snapshot.message_id} "
                f"{mask_email(snapshot.to_email)}"
            )
        if len(snapshots) > 1 or not sys.stdin.isatty():
            return yes
        return typer.confirm("Send now?", default=False)

    global _STOP
    _STOP = False
    previous_handler: object | None = None
    try:
        previous_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _request_stop)
    except (ValueError, AttributeError, OSError):
        previous_handler = None

    @_ledger("send")
    async def _run() -> SendStats:
        return await send_queued(
            limit=limit,
            lead_id=lead_id,
            source=source,
            live=live,
            no_delay=no_delay,
            force_window=force_window,
            confirm=confirm if live else None,
            stop=_stop_requested if live else None,
            max_runtime=max_runtime or 0.0,
        )

    try:
        stats = asyncio.run(_run())
    except SendRefused as exc:
        console.print(f"[red]Not confirmed[/]: {exc}")
        raise typer.Exit(code=2) from exc
    except LockHeldError as exc:
        # Scheduled runs treat "busy" as skip-and-retry, not as an incident.
        console.print(f"[yellow]Another live run is active[/]: {exc}")
        raise typer.Exit(code=75) from exc
    except SendConfigError as exc:
        console.print(f"[red]Mailer setup failed[/]: {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        if previous_handler is not None:
            try:
                signal.signal(signal.SIGTERM, previous_handler)
            except (ValueError, AttributeError, OSError):
                pass
    if json_output:
        print(
            jsonlib.dumps(
                {**asdict(stats), "live": live, "kill_switch": settings.mail_dry_run},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return
    if stats.stop_reason == "kill_switch":
        console.print(
            "[yellow]Kill switch armed[/] (FORGESMITH_MAIL_DRY_RUN=true); "
            "no live message was attempted."
        )
    _print_send_stats(stats, live=live)


def _print_send_stats(stats: SendStats, *, live: bool) -> None:
    table = Table(title="Live send summary" if live else "Dry-run summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for name, value in (
        ("scanned", stats.scanned),
        ("dry-run files" if not live else "sent", stats.sent if live else stats.dry_run),
        ("failed", stats.failed),
        ("bounced", stats.bounced),
        ("uncertain", stats.uncertain),
        ("aborted", stats.aborted),
        ("skipped", stats.skipped),
        ("deferred", stats.deferred),
        ("recovered to uncertain", stats.uncertain_recovered),
        ("uncertain outstanding", stats.uncertain_outstanding),
        ("stop reason", stats.stop_reason or "-"),
        ("duration (s)", stats.duration_s),
    ):
        table.add_row(name, str(value))
    if stats.uncertain:
        console.print(
            "[yellow]uncertain[/] means the relay may have accepted the message; "
            "it is never retried automatically. Resolve with `forgesmith mark --send-id`."
        )
    console.print(table)


@app.command("sends")
def sends(
    status: Annotated[
        list[str] | None, typer.Option("--status", help="Filter by send status.")
    ] = None,
    lead_id: Annotated[int | None, typer.Option(help="Filter by lead id.")] = None,
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 50,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show the attempt ledger, including uncertain and aborted rows."""

    async def run() -> list[dict[str, object]]:
        await init_db()
        async with session_scope() as session:
            rows = await MailRepo(session).list_sends(statuses=status, lead_id=lead_id, limit=limit)
            return [
                {
                    "id": item.id,
                    "lead_id": lead.id,
                    "message_id": message.id,
                    "to": mask_email(item.to_email),
                    "mode": item.mode,
                    "status": item.status,
                    "error": item.error,
                    "created_at": item.created_at,
                    "sent_at": item.sent_at,
                }
                for item, message, lead in rows
            ]

    rows = asyncio.run(run())
    if json_output:
        print(
            jsonlib.dumps(
                {"count": len(rows), "rows": rows},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return

    table = Table(title=f"Sends ({len(rows)})")
    for column in (
        "send",
        "lead",
        "msg",
        "to",
        "mode",
        "status",
        "error",
        "created",
        "sent",
    ):
        table.add_column(column, overflow="fold")
    for row in rows:
        created = (
            row["created_at"].strftime("%Y-%m-%d %H:%M")
            if isinstance(row["created_at"], datetime)
            else "-"
        )
        sent_at = (
            row["sent_at"].strftime("%Y-%m-%d %H:%M")
            if isinstance(row["sent_at"], datetime)
            else "-"
        )
        table.add_row(
            str(row["id"]),
            str(row["lead_id"]),
            str(row["message_id"]),
            str(row["to"]),
            str(row["mode"]),
            str(row["status"]),
            str(row["error"] or "-")[:40],
            created,
            sent_at,
        )
    console.print(table)


@app.command("outbox")
def outbox(
    limit: Annotated[int, typer.Option(help="Files to show.")] = 20,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List rendered .eml evidence files."""
    folder = settings.outbox_dir
    files = sorted(
        folder.glob("*.eml"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:limit]
    rows = [
        {
            "file": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime),
        }
        for path in files
    ]
    if json_output:
        print(
            jsonlib.dumps(
                {"count": len(rows), "dir": str(folder), "rows": rows},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return

    table = Table(title=f"Outbox ({len(files)} of {folder})")
    for column in ("file", "size", "modified"):
        table.add_column(column, overflow="fold")
    for row in rows:
        modified = (
            row["modified"].strftime("%Y-%m-%d %H:%M")
            if isinstance(row["modified"], datetime)
            else "-"
        )
        table.add_row(str(row["file"]), f"{row['size']} B", modified)
    console.print(table)


@app.command("mark")
def mark(
    lead_id: Annotated[int | None, typer.Option("--lead-id", help="Mark a lead outcome.")] = None,
    send_id: Annotated[
        int | None, typer.Option("--send-id", help="Resolve one uncertain/pending send.")
    ] = None,
    status: Annotated[
        str, typer.Option(help="Lead: replied|bounced|sent|suppressed. Send: sent|failed.")
    ] = ...,
    reason: Annotated[str | None, typer.Option(help="Why the outcome was recorded.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm a send resolution.")] = False,
) -> None:
    """Record a reply/bounce manually, or resolve an uncertain send."""
    if bool(lead_id) == bool(send_id):
        console.print("[red]Pass exactly one of --lead-id or --send-id.[/]")
        raise typer.Exit(code=2)

    if lead_id is not None:
        allowed = {"replied", "bounced", "sent", "suppressed"}
        if status not in allowed:
            console.print(f"[red]Lead status must be one of {sorted(allowed)}.[/]")
            raise typer.Exit(code=2)

        async def run_lead() -> None:
            await init_db()
            async with session_scope() as session:
                try:
                    lead = await MailRepo(session).mark_lead(lead_id, status, reason=reason)
                except (ApprovalRefused, LookupError) as exc:
                    console.print(f"[red]Refused[/]: {exc}")
                    raise typer.Exit(code=2) from exc
            console.print(f"[green]Marked[/] lead {lead_id} as {lead.status}")

        asyncio.run(run_lead())
        return

    assert send_id is not None
    if status not in {"sent", "failed"}:
        console.print("[red]Send status must be sent or failed.[/]")
        raise typer.Exit(code=2)
    if not yes and not sys.stdin.isatty():
        console.print("[red]--yes is required to resolve a send non-interactively.[/]")
        raise typer.Exit(code=2)

    async def run_send() -> None:
        await init_db()
        async with session_scope() as session:
            try:
                row = await MailRepo(session).mark_send(send_id, status, reason=reason)
            except (ApprovalRefused, LookupError) as exc:
                console.print(f"[red]Refused[/]: {exc}")
                raise typer.Exit(code=2) from exc
        console.print(f"[green]Send {send_id} marked {row.status}[/]")

    asyncio.run(run_send())


@app.command("unsuppress")
def unsuppress(
    email: Annotated[
        str | None, typer.Option("--email", help="Remove an email suppression.")
    ] = None,
    domain: Annotated[
        str | None, typer.Option("--domain", help="Remove a domain suppression.")
    ] = None,
    reason: Annotated[str | None, typer.Option(help="Why the reversal was recorded.")] = None,
) -> None:
    """Reverse a suppression (automatic suppressions must be reversible)."""
    if bool(email) == bool(domain):
        console.print("[red]Pass exactly one of --email or --domain.[/]")
        raise typer.Exit(code=1)
    value = (email or domain or "").strip().lower()
    kind = "email" if email else "domain"

    async def run() -> None:
        await init_db()
        async with session_scope() as session:
            try:
                await MailRepo(session).unsuppress(value=value, kind=kind, reason=reason)
            except LookupError as exc:
                console.print(f"[red]{exc}[/]")
                raise typer.Exit(code=2) from exc
        console.print(f"[green]Unsuppressed[/] {kind}: {value}")

    asyncio.run(run())


# ------------------------------------------------------ phase 5 operations
def _sqlite_path() -> Path | None:
    """Filesystem path behind ``database_url`` for SQLite, else ``None``."""
    if not settings.is_sqlite:
        return None
    tail = settings.database_url.partition("///")[2]
    if not tail or tail == ":memory:":
        return None
    return Path(tail)


def _outbox_usage() -> tuple[int, int]:
    folder = settings.outbox_dir
    if not folder.exists():
        return 0, 0
    files = list(folder.glob("*.eml"))
    return len(files), sum(path.stat().st_size for path in files)


def _window_state() -> dict[str, object]:
    try:
        policy = MailPolicy.from_settings(settings)
    except SendConfigError as exc:
        return {"open": None, "error": str(exc)}
    now = datetime.now(UTC)
    return {
        "open": policy.window.is_open(now),
        "next_opening": policy.window.next_opening(now).isoformat(),
        "timezone": settings.mail_window_tz,
        "daily_cap": policy.daily_cap,
    }


def _ollama_state() -> dict[str, object]:
    """TCP probe of the Ollama daemon.

    A connect is enough to answer "is the Draft stage usable"; it costs a few
    milliseconds when the daemon is up and fails immediately on localhost when
    it is not. No HTTP call, so no model is loaded and nothing is logged.
    """
    parsed = urlparse(settings.ollama_base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 11434
    state: dict[str, object] = {
        "url": settings.ollama_base_url,
        "model": settings.ollama_model,
    }
    try:
        with socket.create_connection((host, port), timeout=0.3):
            state["reachable"] = True
    except OSError as exc:
        state["reachable"] = False
        state["error"] = type(exc).__name__
    return state


def _blockers(window: dict[str, object]) -> dict[str, dict[str, object]]:
    """What currently stops a stage from doing anything, with the reason.

    The GUI renders these on the page that owns them instead of leaving the
    operator to discover a closed window or a stopped Ollama by watching a run
    do nothing.
    """
    armed = bool(settings.mail_dry_run)
    window_open = window.get("open")
    if window_open is True:
        window_detail = f"open until {settings.mail_window_end}:00 {settings.mail_window_tz}"
    elif window_open is False:
        window_detail = f"closed; opens {window.get('next_opening') or 'later'}"
    else:
        window_detail = str(window.get("error") or "window configuration is invalid")

    sender_blocked = False
    sender_detail = "sender identity is configured"
    try:
        SenderProfile.from_settings(settings)
    except ComposeConfigError as exc:
        sender_blocked = True
        sender_detail = str(exc)

    ollama = _ollama_state()
    ollama_blocked = not ollama["reachable"]
    ollama_detail = (
        f"Ollama answers at {ollama['url']} (model {ollama['model']})"
        if not ollama_blocked
        else f"Ollama is not listening at {ollama['url']} — start it before drafting"
    )

    return {
        "kill_switch": {
            "blocked": armed,
            "detail": (
                "kill switch armed (FORGESMITH_MAIL_DRY_RUN=true): live send is disabled"
                if armed
                else "kill switch off: live send is allowed"
            ),
        },
        "window": {"blocked": window_open is not True, "detail": window_detail},
        "ollama": {"blocked": ollama_blocked, "detail": ollama_detail},
        "sender": {"blocked": sender_blocked, "detail": sender_detail},
    }


def _run_summary(latest: dict[str, Event]) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for stage, event in latest.items():
        detail = event.detail or {}
        at = ensure_utc(event.created_at) or datetime.now(UTC)
        summary[stage] = {
            "at": at.isoformat(),
            "exit_code": detail.get("exit_code"),
            "duration_s": detail.get("duration_s"),
            "stop_reason": detail.get("stop_reason"),
            "error": detail.get("error"),
        }
    return summary


@app.command("status")
def status(
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """One screen: pipeline counts, window, kill switch, and recent runs."""

    async def main() -> dict[str, object]:
        await init_db()
        async with session_scope() as session:
            leads = LeadRepo(session)
            mail = MailRepo(session)
            inbox = InboxRepo(session)
            window = _window_state()
            funnel = await leads.funnel(
                compose_max_attempts=settings.compose_max_attempts,
                enrich_max_attempts=settings.enrich_max_attempts,
            )
            inbox_counts = await inbox.inbound_status_counts()
            payload: dict[str, object] = {
                "database": settings.database_url,
                "data_dir": str(settings.data_dir),
                "kill_switch": "armed" if settings.mail_dry_run else "off",
                "approval_channel": settings.approval_channel or "interactive",
                "window": window,
                "blockers": _blockers(window),
                "funnel": funnel,
                "inbox": inbox_counts,
                "leads": {
                    "total": await leads.count(),
                    "by_status": await leads.status_counts(),
                    "by_source": await leads.source_counts(),
                },
                "sends": await mail.send_status_counts(),
                "uncertain": await mail.count_uncertain(),
                "runs": _run_summary(await mail.latest_run_per_stage()),
            }
        files, size = _outbox_usage()
        payload["outbox"] = {"files": files, "bytes": size}
        return payload

    payload = asyncio.run(main())
    if json_output:
        print(jsonlib.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    leads = payload["leads"]
    sends = payload["sends"]
    window = payload["window"]
    assert isinstance(leads, dict)
    assert isinstance(sends, dict)
    assert isinstance(window, dict)
    table = Table(title="Pipeline status")
    table.add_column("metric", overflow="fold")
    table.add_column("value", overflow="fold")
    table.add_row("database", str(payload["database"]))
    table.add_row("kill switch", str(payload["kill_switch"]))
    table.add_row("approval channel", str(payload["approval_channel"]))
    table.add_row(
        "window",
        f"{'open' if window.get('open') else 'closed'} (next {window.get('next_opening', '-')})",
    )
    funnel = payload.get("funnel") or {}
    assert isinstance(funnel, dict)
    table.add_row("leads", str(leads.get("total")))
    for label, key in (
        ("collect", "collected"),
        ("awaiting enrich", "enrichable"),
        ("ready to draft", "composable"),
        ("drafted", "drafted"),
        ("approved", "approved"),
        ("parked", "parked"),
        ("domainless", "domainless"),
    ):
        table.add_row(f"  funnel: {label}", str(funnel.get(key, 0)))
    inbox = payload.get("inbox") or {}
    assert isinstance(inbox, dict)
    table.add_row("  inbox: needs review", str(inbox.get("needs_review", 0)))
    for name, count in sorted(
        dict(leads.get("by_status") or {}).items(), key=lambda item: -int(item[1])
    ):
        table.add_row(f"  lead: {name}", str(count))
    for name, count in sorted(
        dict(leads.get("by_source") or {}).items(), key=lambda item: -int(item[1])
    ):
        table.add_row(f"  source: {name}", str(count))
    for name, count in sorted(dict(sends).items(), key=lambda item: -int(item[1])):
        table.add_row(f"  send: {name}", str(count))
    table.add_row("uncertain outstanding", str(payload["uncertain"]))
    outbox = payload["outbox"]
    assert isinstance(outbox, dict)
    table.add_row("outbox", f"{outbox.get('files')} files, {outbox.get('bytes')} bytes")
    blockers = payload.get("blockers") or {}
    assert isinstance(blockers, dict)
    for name, info in blockers.items():
        assert isinstance(info, dict)
        table.add_row(
            f"blocker: {name}",
            ("BLOCKED " if info.get("blocked") else "ok ") + str(info.get("detail") or ""),
        )
    runs = payload["runs"]
    assert isinstance(runs, dict)
    for stage, info in runs.items():
        assert isinstance(info, dict)
        table.add_row(
            f"run: {stage}",
            f"{info.get('at')} exit={info.get('exit_code')} "
            f"({info.get('duration_s')}s) stop={info.get('stop_reason') or '-'} "
            f"{info.get('error') or ''}".strip(),
        )
    console.print(table)


@app.command("doctor")
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    smtp_check: Annotated[
        bool,
        typer.Option("--smtp-check", help="Also connect, STARTTLS and log in to SMTP."),
    ] = False,
) -> None:
    """Pre-flight checks for containerized/scheduled operation (exit 1 fail, 3 warn)."""

    async def main() -> list[dict[str, str]]:
        results: list[dict[str, str]] = []

        def add(check: str, state: str, detail: str) -> None:
            results.append({"check": check, "status": state, "detail": detail})

        try:
            await init_db()
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
                counts = await MailRepo(session).send_status_counts()
            add("database", "ok", f"{settings.database_url} ({sum(counts.values())} sends)")
        except Exception as exc:
            add("database", "fail", f"{type(exc).__name__}: {exc}")

        for name, folder in (
            ("data_dir", settings.data_dir),
            ("outbox", settings.outbox_dir),
            ("backups", settings.backups_dir),
        ):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                probe = folder / ".doctor-probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                add(name, "ok", str(folder))
            except OSError as exc:
                add(name, "fail", f"{folder}: {exc}")

        try:
            SenderProfile.from_settings(settings)
            add(
                "sender",
                "ok",
                f"{settings.mail_from_name} <{settings.mail_from_address}>",
            )
        except ComposeConfigError as exc:
            add("sender", "fail", str(exc))

        try:
            MailPolicy.from_settings(settings)
            add(
                "timezone",
                "ok",
                f"{settings.mail_window_tz} "
                f"{settings.mail_window_start}:00-{settings.mail_window_end}:00",
            )
        except SendConfigError as exc:
            add("timezone", "fail", str(exc))

        add(
            "kill_switch",
            "info",
            "armed (FORGESMITH_MAIL_DRY_RUN=true)"
            if settings.mail_dry_run
            else "off (live allowed)",
        )
        add("approval_channel", "info", settings.approval_channel or "interactive")

        url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(url)
            state = "ok" if response.status_code < 500 else "warn"
            add("ollama", state, f"{url} (HTTP {response.status_code})")
        except Exception as exc:
            add("ollama", "warn", f"{url} unreachable: {type(exc).__name__}")

        try:
            usage = shutil.disk_usage(settings.data_dir)
            state = "ok" if usage.free > 500_000_000 else "warn"
            add("disk", state, f"{usage.free / 1e9:.1f} GB free")
        except OSError as exc:
            add("disk", "fail", str(exc))

        lock = settings.mailer_lock_path
        if lock.exists():
            holder = lock.read_text("ascii", errors="ignore").strip() or "?"
            add("send_lock", "info", f"{lock.name} held by pid {holder}")
        else:
            add("send_lock", "ok", "free")

        host_now = os.environ.get("FORGESMITH_HOST_NOW")
        if host_now:
            try:
                skew = abs(
                    (
                        datetime.now(UTC)
                        - (ensure_utc(datetime.fromisoformat(host_now)) or datetime.now(UTC))
                    ).total_seconds()
                )
                add("clock_skew", "ok" if skew < 120 else "warn", f"{skew:.0f}s vs host")
            except ValueError:
                add("clock_skew", "warn", "unparseable FORGESMITH_HOST_NOW")
        else:
            add("clock_skew", "info", "FORGESMITH_HOST_NOW not set")

        try:
            async with session_scope() as session:
                latest = await MailRepo(session).latest_run_per_stage()
            now = datetime.now(UTC)
            for stage, event in latest.items():
                detail = event.detail or {}
                at = ensure_utc(event.created_at) or now
                age = (now - at).total_seconds()
                stale = age > settings.stage_stale_after
                bad = detail.get("exit_code") not in (0, None) or bool(detail.get("error"))
                add(
                    f"run:{stage}",
                    "warn" if stale or bad else "ok",
                    f"{int(age)}s ago exit={detail.get('exit_code')} "
                    f"{detail.get('error') or ''}".strip(),
                )
        except Exception as exc:
            add("run_ledger", "fail", f"{type(exc).__name__}: {exc}")

        try:
            async with session_scope() as session:
                uncertain = await MailRepo(session).count_uncertain()
            add("uncertain", "warn" if uncertain else "ok", f"{uncertain} unresolved")
        except Exception as exc:
            add("uncertain", "fail", f"{type(exc).__name__}: {exc}")

        if smtp_check:
            transport = SmtpTransport(settings)
            try:
                await transport.open()
                add("smtp", "ok", f"{settings.smtp_host}:{settings.smtp_port}")
            except TransportError as exc:
                add("smtp", "fail", str(exc))
            finally:
                await transport.close()

        return results

    results = asyncio.run(main())
    failures = [row for row in results if row["status"] == "fail"]
    warnings = [row for row in results if row["status"] == "warn"]
    if json_output:
        print(
            jsonlib.dumps(
                {"ok": not failures, "checks": results},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        table = Table(title="Doctor")
        table.add_column("check", overflow="fold")
        table.add_column("status")
        table.add_column("detail", overflow="fold")
        for row in results:
            colour = {"ok": "green", "warn": "yellow", "fail": "red"}.get(row["status"], "cyan")
            table.add_row(row["check"], f"[{colour}]{row['status']}[/]", row["detail"])
        console.print(table)
    if failures:
        raise typer.Exit(code=1)
    if warnings:
        raise typer.Exit(code=3)


def _prune_backups(folder: Path, keep: int) -> list[Path]:
    if keep <= 0:
        return []
    snapshots = sorted(folder.glob("forgesmith-*.db"), reverse=True)
    removed: list[Path] = []
    for stale in snapshots[keep:]:
        stale.unlink(missing_ok=True)
        removed.append(stale)
    return removed


def _run_backup(out: Path | None, keep: int, with_outbox: bool) -> None:
    """Atomically snapshot the SQLite database with ``VACUUM INTO``.

    Safe while other stages are running: SQLite takes a consistent read
    transaction, so no writer has to be stopped.
    """
    source = _sqlite_path()
    if source is None:
        console.print(
            "[red]backup supports the SQLite production path only[/] "
            "(unset FORGESMITH_DATABASE_URL or point it at a file)."
        )
        raise typer.Exit(code=1)
    if not source.exists():
        console.print(f"[red]Database not found[/]: {source}")
        raise typer.Exit(code=1)

    target_dir = (out or settings.backups_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snapshot = target_dir / f"forgesmith-{stamp}.db"
    if snapshot.exists():
        console.print(f"[red]Snapshot already exists[/]: {snapshot}")
        raise typer.Exit(code=1)

    quoted = snapshot.as_posix().replace("'", "''")
    try:
        connection = sqlite3.connect(source)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute(f"VACUUM INTO '{quoted}'")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        console.print(f"[red]Snapshot failed[/]: {exc}")
        raise typer.Exit(code=1) from exc

    check = sqlite3.connect(snapshot)
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
        integrity = row[0] if row else "unknown"
    finally:
        check.close()
    if integrity != "ok":
        console.print(f"[red]Snapshot is not consistent[/]: {integrity}")
        raise typer.Exit(code=1)

    archived: Path | None = None
    files, _ = _outbox_usage()
    if with_outbox and files:
        archived = target_dir / f"outbox-{stamp}.tar.gz"
        with tarfile.open(archived, "w:gz") as tar:
            for path in sorted(settings.outbox_dir.glob("*.eml")):
                tar.add(path, arcname=path.name)

    removed = _prune_backups(target_dir, keep)
    console.print(
        f"[green]Snapshot ok[/]: {snapshot} ({snapshot.stat().st_size} bytes, "
        f"integrity {integrity})"
    )
    if archived is not None:
        console.print(f"[green]Outbox archive[/]: {archived} ({files} files)")
    if removed:
        console.print(f"Pruned {len(removed)} old snapshot(s)")


@app.command("backup")
def backup(
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Snapshot directory (default: data/backups)."),
    ] = None,
    keep: Annotated[int, typer.Option(help="Keep only the newest N snapshots (0 keeps all).")] = 0,
    with_outbox: Annotated[
        bool,
        typer.Option("--outbox/--no-outbox", help="Also archive rendered .eml evidence."),
    ] = True,
) -> None:
    """Snapshot the database (and optionally the outbox) for disaster recovery."""
    asyncio.run(_run_ledger_wrapped(out, keep, with_outbox))


@_ledger("backup")
async def _run_ledger_wrapped(out: Path | None, keep: int, with_outbox: bool) -> None:
    _run_backup(out, keep, with_outbox)


# ------------------------------------------------------- phase 4.5 inbox
inbox_app = typer.Typer(help="Read-only IMAP ingest of bounces and replies.")
app.add_typer(inbox_app, name="inbox")


@inbox_app.command("sync")
def inbox_sync(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Fetch and parse, but write nothing.")
    ] = False,
    limit: Annotated[int | None, typer.Option(help="Max messages this pass.")] = None,
) -> None:
    """Read new mail; apply hard bounces and thread-matched replies."""

    @_ledger("inbox")
    async def run() -> object:
        return await sync_inbox(dry_run=dry_run, limit=limit)

    try:
        stats = asyncio.run(run())
    except LockHeldError as exc:
        console.print(f"[yellow]Another inbox sync is active[/]: {exc}")
        raise typer.Exit(code=75) from exc
    except (InboxConfigError, InboxError) as exc:
        console.print(f"[red]Inbox sync failed[/]: {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="Inbox sync (dry run)" if dry_run else "Inbox sync")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for name, value in (
        ("scanned", stats.scanned),
        ("ingested", stats.ingested),
        ("applied", stats.applied),
        ("bounces", stats.bounces),
        ("replies", stats.replies),
        ("needs review", stats.needs_review),
        ("ignored", stats.ignored),
        ("duplicates", stats.duplicates),
        ("failed", stats.failed),
        ("dry-run parsed", stats.dry_run),
        ("uidvalidity reset", stats.uidvalidity_reset),
        ("duration (s)", stats.duration_s),
    ):
        table.add_row(name, str(value))
    console.print(table)
    if stats.needs_review:
        console.print(
            "[yellow]needs review[/] rows are never applied automatically; inspect with "
            "`forgesmith inbox list --status needs_review`."
        )


@inbox_app.command("list")
def inbox_list(
    status: Annotated[list[str] | None, typer.Option("--status", help="Filter by status.")] = None,
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 50,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show the inbound ledger, newest first."""

    async def run() -> list[dict[str, object]]:
        await init_db()
        async with session_scope() as session:
            rows = list(await InboxRepo(session).list_inbound(statuses=status, limit=limit))
            return [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "status": row.status,
                    "from": mask_email(row.from_addr or "-"),
                    "subject": row.subject or "-",
                    "lead_id": row.lead_id,
                    "send_id": row.send_id,
                    "enhanced_status": row.enhanced_status,
                    "seen_at": row.seen_at,
                    "applied_at": row.applied_at,
                }
                for row in rows
            ]

    rows = asyncio.run(run())
    if json_output:
        print(
            jsonlib.dumps(
                {"count": len(rows), "rows": rows},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return

    table = Table(title=f"Inbound ({len(rows)})")
    for column in ("id", "kind", "status", "from", "subject", "lead", "send", "seen"):
        table.add_column(column, overflow="fold")
    for row in rows:
        seen = (
            row["seen_at"].strftime("%Y-%m-%d %H:%M")
            if isinstance(row["seen_at"], datetime)
            else "-"
        )
        table.add_row(
            str(row["id"]),
            str(row["kind"]),
            str(row["status"]),
            str(row["from"]),
            str(row["subject"])[:50],
            str(row["lead_id"] or "-"),
            str(row["send_id"] or "-"),
            seen,
        )
    console.print(table)


@inbox_app.command("show")
def inbox_show(
    inbound_id: Annotated[int, typer.Option("--id", help="Inbound ledger id.")],
) -> None:
    """Explain one inbound message and the action it proposes."""

    async def run() -> object | None:
        await init_db()
        async with session_scope() as session:
            return await InboxRepo(session).get_inbound(inbound_id)

    row = asyncio.run(run())
    if row is None:
        console.print(f"[red]No inbound message {inbound_id}[/]")
        raise typer.Exit(code=2)

    table = Table(title=f"Inbound {row.id}")
    table.add_column("field", overflow="fold")
    table.add_column("value", overflow="fold")
    for name, value in (
        ("kind", row.kind),
        ("status", row.status),
        ("from", mask_email(row.from_addr or "-")),
        ("to", mask_email(row.to_addr or "-")),
        ("final recipient", mask_email(row.final_recipient or "-")),
        ("subject", row.subject or "-"),
        ("message-id", row.rfc_message_id or "-"),
        ("reported message-id", row.reported_message_id or "-"),
        ("enhanced status", row.enhanced_status or "-"),
        ("smtp code", str(row.smtp_code or "-")),
        ("lead", str(row.lead_id or "-")),
        ("send", str(row.send_id or "-")),
        ("received", row.received_at.isoformat() if row.received_at else "-"),
        ("seen", row.seen_at.isoformat() if row.seen_at else "-"),
        ("applied", row.applied_at.isoformat() if row.applied_at else "-"),
    ):
        table.add_row(name, str(value))
    console.print(table)
    console.print(jsonlib.dumps(row.detail or {}, ensure_ascii=False, indent=2, default=str))
    if row.kind == InboundKind.OPTOUT and row.status == InboundStatus.NEEDS_REVIEW:
        console.print(
            f"[yellow]Opt-out[/]: confirm with `forgesmith inbox apply --id {row.id} --yes` "
            "(email suppression only) or leave it unapplied."
        )
    elif row.status == InboundStatus.NEEDS_REVIEW:
        console.print(
            "[yellow]No safe automatic action[/]: attribute it with "
            "`forgesmith mark --lead-id ...` or `forgesmith suppress --email ...`."
        )


@inbox_app.command("apply")
def inbox_apply(
    inbound_id: Annotated[int | None, typer.Option("--id", help="Apply one ledger row.")] = None,
    all_rows: Annotated[bool, typer.Option("--all", help="Apply every `new` row.")] = False,
    limit: Annotated[int, typer.Option(help="Max rows for --all.")] = 200,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm a bulk apply or an opt-out.")] = False,
) -> None:
    """Apply a stored outcome. Opt-out rows require ``--yes``."""
    if bool(inbound_id) == bool(all_rows):
        console.print("[red]Pass exactly one of --id or --all.[/]")
        raise typer.Exit(code=2)

    if inbound_id is not None:

        async def run_one() -> tuple[object | None, bool]:
            await init_db()
            async with session_scope() as session:
                repo = InboxRepo(session)
                row = await repo.get_inbound(inbound_id)
                if row is None:
                    return None, False
                return row, await repo.apply_inbound(row, allow_optout=yes)

        row, applied = asyncio.run(run_one())
        if row is None:
            console.print(f"[red]No inbound message {inbound_id}[/]")
            raise typer.Exit(code=2)
        if applied:
            console.print(f"[green]Applied[/] inbound {inbound_id} ({row.kind})")
            return
        if row.kind == InboundKind.OPTOUT and not yes:
            console.print("[yellow]Opt-out rows need --yes[/] (email suppression only).")
            raise typer.Exit(code=2)
        console.print(
            f"[yellow]Nothing applied[/]: inbound {inbound_id} is {row.status} ({row.kind})."
        )
        raise typer.Exit(code=2)

    async def list_ids() -> list[int]:
        await init_db()
        async with session_scope() as session:
            rows = await InboxRepo(session).list_inbound(statuses=[InboundStatus.NEW], limit=limit)
            return [row.id for row in rows]

    ids = asyncio.run(list_ids())
    if not ids:
        console.print("No `new` inbound rows to apply.")
        return
    _confirm_bulk(len(ids), yes, "Bulk inbox apply")

    async def apply_all() -> int:
        applied = 0
        for candidate in ids:
            async with session_scope() as session:
                repo = InboxRepo(session)
                row = await repo.get_inbound(candidate)
                if row is not None and await repo.apply_inbound(row):
                    applied += 1
        return applied

    applied = asyncio.run(apply_all())
    console.print(f"[green]applied {applied}[/], skipped {len(ids) - applied}")


if __name__ == "__main__":  # pragma: no cover
    app()
