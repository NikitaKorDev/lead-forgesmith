"""CLI entry point: ``forgesmith <group> <command>``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import structlog
import typer
from rich.console import Console
from rich.table import Table

from forgesmith import __version__
from forgesmith.browser import BrowserFetcher
from forgesmith.config import settings
from forgesmith.console import force_utf8_stdio
from forgesmith.db import EnrichmentRepo, LeadRepo, init_db, session_scope
from forgesmith.domain import LeadCandidate, UpsertStats
from forgesmith.enrichment import RunStats, enrich_pending
from forgesmith.http import PoliteClient
from forgesmith.logs import configure_logging
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


@app.callback()
def bootstrap(
    log_level: Annotated[
        str, typer.Option("--log-level", help="debug | info | warning | error")
    ] = "info",
) -> None:
    """Global options."""
    force_utf8_stdio()
    configure_logging(log_level)


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
    paths = seeds or [settings.shopify_seeds_file]
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
        _consume(lambda client: ShopifySource(client, seeds=seed_list, limit=limit), "shopify")
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
        _consume(
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
) -> None:
    """List stored leads."""

    async def main() -> None:
        await init_db()
        async with session_scope() as session:
            repo = LeadRepo(session)
            rows = await repo.list(limit=limit, status=status, source=source)
            total = await repo.count()

        table = Table(title=f"Leads (showing {len(rows)} of {total})")
        for column in ("id", "source", "name", "domain", "city", "category", "status"):
            table.add_column(column, overflow="fold")
        for lead in rows:
            table.add_row(
                str(lead.id),
                lead.source,
                lead.name,
                lead.domain or "-",
                lead.city or "-",
                lead.category or "-",
                lead.status,
            )
        console.print(table)

    asyncio.run(main())


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
) -> None:
    """Resolve storefronts, extract company emails and context."""
    if engine not in {"auto", "http", "browser"}:
        console.print(f"[red]Unknown engine: {engine}[/] (use auto, http, or browser)")
        raise typer.Exit(code=1)

    stats = asyncio.run(
        enrich_pending(
            limit=limit,
            source=source,
            lead_id=lead_id,
            force=force,
            resolve_only=resolve_only,
            deep_resolve=deep_resolve,
            engine=engine,
        )
    )
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
) -> None:
    """Show observed contacts that Phase 3 will use."""

    async def main() -> None:
        await init_db()
        async with session_scope() as session:
            rows = await EnrichmentRepo(session).list_contacts(limit=limit, lead_id=lead_id)

        table = Table(title=f"Contacts ({len(rows)})")
        for column in ("lead", "name", "email", "kind", "confidence", "source"):
            table.add_column(column, overflow="fold")
        for contact, lead in rows:
            table.add_row(
                str(lead.id),
                lead.name,
                contact.email,
                contact.kind,
                f"{contact.confidence:.2f}",
                contact.source_url or "-",
            )
        console.print(table)

    asyncio.run(main())


@app.command("version")
def version() -> None:
    """Print the version."""
    console.print(f"forgesmith {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
