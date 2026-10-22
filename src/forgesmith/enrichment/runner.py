"""Sequential enrichment runner: resolve, crawl, extract, persist."""

from __future__ import annotations

import re
import time
from typing import Any

import structlog

from forgesmith.config import Settings
from forgesmith.config import settings as default_settings
from forgesmith.db import EnrichmentRepo, LeadRepo, init_db, session_scope
from forgesmith.db.models import EnrichmentStatusDB, LeadStatusDB
from forgesmith.enrichment.emails import select_contacts
from forgesmith.enrichment.fetch import SiteFetcher
from forgesmith.enrichment.identity import find_legal_name, find_nip
from forgesmith.enrichment.models import EnrichmentResult, PageSnapshot, RunStats
from forgesmith.enrichment.resolve import ResolvedDomain, load_overrides, resolve_domain
from forgesmith.enrichment.site import (
    detect_platform,
    is_challenge_page,
    is_parked_page,
    main_excerpt,
    parse_html,
    product_titles,
    rank_links,
)

log = structlog.get_logger(__name__)

_ABOUT_PATH = re.compile(r"o-nas|o-firmie|about|firma|historia|o_mnie", re.IGNORECASE)
_BLOCK_STATUS = {401, 403, 429, 503}


class EnrichmentRunner:
    def __init__(
        self,
        fetcher: SiteFetcher,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.settings = settings or default_settings

    async def run(
        self,
        *,
        limit: int | None = None,
        source: str | None = None,
        lead_id: int | None = None,
        force: bool = False,
        resolve_only: bool = False,
        deep_resolve: bool = False,
    ) -> RunStats:
        stats = RunStats()
        started = time.monotonic()

        async with session_scope(self.settings) as session:
            leads = list(
                await LeadRepo(session).select_pending(
                    limit=limit,
                    source=source,
                    lead_id=lead_id,
                    force=force,
                    max_attempts=self.settings.enrich_max_attempts,
                )
            )
            tracked = await LeadRepo(session).domains()

        overrides = load_overrides(self.settings.enrich_domain_overrides)
        for lead in leads:
            stats.scanned += 1
            deadline = time.monotonic() + self.settings.enrich_lead_timeout
            try:
                await self._process(
                    lead,
                    stats=stats,
                    tracked=tracked,
                    overrides=overrides,
                    deadline=deadline,
                    resolve_only=resolve_only,
                    deep_resolve=deep_resolve,
                )
            except Exception as exc:  # one bad site must not abort the run
                log.warning("enrich.lead_failed", lead_id=getattr(lead, "id", None))
                await self._finish(
                    lead,
                    EnrichmentResult(
                        status=EnrichmentStatusDB.ERROR,
                        reason=f"exception:{type(exc).__name__}",
                        last_error=str(exc)[:500],
                        evidence={"exception": type(exc).__name__},
                    ),
                    stats,
                )

        stats.duration_s = round(time.monotonic() - started, 2)
        return stats

    # ---------------------------------------------------------------- per lead
    async def _process(
        self,
        lead: Any,
        *,
        stats: RunStats,
        tracked: dict[str, int],
        overrides: dict[int, str],
        deadline: float,
        resolve_only: bool,
        deep_resolve: bool,
    ) -> None:
        lead_id = lead.id
        if not resolve_only:
            # Claim before any network I/O so an interrupted run still advances
            # the attempt counter and a poison lead eventually reaches skipped.
            async with session_scope(self.settings) as session:
                await EnrichmentRepo(session).claim_attempt(lead_id)

        resolved = await resolve_domain(
            lead,
            self.fetcher,
            overrides,
            deep=deep_resolve,
            deadline=deadline,
        )

        if resolved is None:
            result = _result_for_resolution(
                None,
                evidence={"reason": "domain_unresolved"},
            )
            if resolve_only:
                await self._save_resolution(lead_id, result)
                stats.no_site += 1
            else:
                await self._finish(lead, result, stats)
            return

        stats.resolved += 1
        if resolved.source == "override":
            stats.overridden += 1

        canonical = tracked.get(resolved.domain)
        if canonical is not None and canonical != lead_id:
            evidence = {
                **resolved.evidence,
                "reason": "duplicate_domain",
                "canonical_lead_id": canonical,
            }
            if resolve_only:
                await self._save_resolution(
                    lead_id,
                    _result_for_resolution(resolved, evidence=evidence),
                )
            else:
                result = _result_for_resolution(resolved, evidence=evidence)
                result.status = EnrichmentStatusDB.NO_SITE
                result.reason = "duplicate_domain"
                await self._finish(lead, result, stats)
            stats.duplicates += 1
            return

        if resolve_only:
            await self._save_resolution(
                lead_id,
                _result_for_resolution(resolved, evidence=resolved.evidence),
            )
            tracked[resolved.domain] = lead_id
            return

        result = await self._crawl(lead, resolved, deadline)
        await self._finish(lead, result, stats)
        if (
            result.promote_domain
            and result.domain
            and result.status in (EnrichmentStatusDB.OK, EnrichmentStatusDB.NO_CONTACT)
        ):
            tracked[result.domain] = lead_id

    async def _crawl(
        self,
        lead: Any,
        resolved: ResolvedDomain,
        deadline: float,
    ) -> EnrichmentResult:
        homepage = await self.fetcher.fetch(resolved.site_url, deadline=deadline)
        if not homepage.ok:
            return _failure_result(resolved, homepage)

        full = parse_html(homepage.html, homepage.final_url)
        if is_parked_page(homepage.html, full.text) or len(full.text.strip()) < 80:
            return _result_for_resolution(
                resolved,
                status=EnrichmentStatusDB.NO_SITE,
                reason="parked_or_empty",
                evidence={**resolved.evidence, "reason": "parked_or_empty"},
            )

        pages_full: list[tuple[str, Any]] = [(homepage.final_url, full)]
        pages_main: list[tuple[str, Any]] = [
            (homepage.final_url, parse_html(homepage.html, homepage.final_url, skip_layout=True))
        ]
        pages_meta: list[dict[str, Any]] = [_page_meta(homepage, text_chars=len(full.text))]
        engines = {homepage.engine}

        remaining = max(0, self.settings.enrich_max_pages - 1)
        for link in rank_links(homepage.final_url, full, limit=remaining):
            if time.monotonic() >= deadline:
                pages_meta.append({"url": link, "error": "deadline"})
                break
            snapshot = await self.fetcher.fetch(link, deadline=deadline)
            engines.add(snapshot.engine)
            if not snapshot.ok:
                pages_meta.append(_page_meta(snapshot))
                continue
            pages_full.append((snapshot.final_url, parse_html(snapshot.html, snapshot.final_url)))
            pages_main.append(
                (
                    snapshot.final_url,
                    parse_html(snapshot.html, snapshot.final_url, skip_layout=True),
                )
            )
            pages_meta.append(_page_meta(snapshot))

        contacts, rejected = select_contacts(
            pages_full,
            resolved.domain,
            limit=self.settings.enrich_email_limit,
        )

        selected_main = [
            page
            for page in pages_main
            if page[0] == homepage.final_url or _ABOUT_PATH.search(page[0])
        ] or pages_main[:1]
        description = next(
            (page.description for _url, page in pages_full if page.description),
            None,
        )
        nip = find_nip(pages_full)
        legal_name = find_legal_name(pages_full, nip["value"] if nip else None)
        products = _product_sample(lead) or product_titles(full)
        context = {
            "text_excerpt": main_excerpt(selected_main, self.settings.enrich_text_chars),
            "headings": full.headings[:20],
            "description": description,
            "products": products,
            "pages": pages_meta,
        }
        evidence: dict[str, Any] = {
            **resolved.evidence,
            "pages": pages_meta,
            "emails_rejected": rejected,
        }
        if nip:
            evidence["nip"] = nip

        status = EnrichmentStatusDB.OK if contacts else EnrichmentStatusDB.NO_CONTACT
        return EnrichmentResult(
            status=status,
            domain=resolved.domain,
            domain_source=resolved.source,
            site_url=homepage.final_url,
            storefront_platform=detect_platform(homepage.html, homepage.headers),
            legal_name=legal_name,
            nip=nip["value"] if nip else None,
            description=description,
            context=context,
            evidence=evidence,
            contacts=contacts,
            engine=_engine_label(engines),
            promote_domain=getattr(lead, "domain", None) is None,
            reason=None if contacts else "no_contact",
        )

    async def _finish(
        self,
        lead: Any,
        result: EnrichmentResult,
        stats: RunStats,
    ) -> str | None:
        async with session_scope(self.settings) as session:
            outcome = await EnrichmentRepo(session).finish(
                lead.id,
                result,
                max_attempts=self.settings.enrich_max_attempts,
                email_limit=self.settings.enrich_email_limit,
            )

        if outcome.lead_status == LeadStatusDB.ENRICHED:
            stats.enriched += 1
            stats.emails += outcome.stored_contacts
        else:
            stats.skipped += 1 if outcome.lead_status == LeadStatusDB.SKIPPED else 0
            status = outcome.enrichment_status
            stats.failed += 1 if status == EnrichmentStatusDB.ERROR else 0
            if status == EnrichmentStatusDB.NO_SITE:
                stats.no_site += 1
            elif status == EnrichmentStatusDB.NO_CONTACT:
                stats.no_contact += 1
            elif status == EnrichmentStatusDB.BLOCKED:
                stats.blocked += 1
        return outcome.lead_status

    async def _save_resolution(self, lead_id: int, result: EnrichmentResult) -> None:
        async with session_scope(self.settings) as session:
            await EnrichmentRepo(session).save_resolution(
                lead_id,
                domain=result.domain,
                source=result.domain_source,
                evidence=result.evidence,
            )


async def enrich_pending(
    *,
    limit: int | None = None,
    source: str | None = None,
    lead_id: int | None = None,
    force: bool = False,
    resolve_only: bool = False,
    deep_resolve: bool = False,
    engine: str = "auto",
    settings: Settings | None = None,
    fetcher: SiteFetcher | None = None,
) -> RunStats:
    """Public entry point used by the CLI and tests."""
    cfg = settings or default_settings
    await init_db(cfg)
    if fetcher is not None:
        return await EnrichmentRunner(fetcher, settings=cfg).run(
            limit=limit,
            source=source,
            lead_id=lead_id,
            force=force,
            resolve_only=resolve_only,
            deep_resolve=deep_resolve,
        )
    async with SiteFetcher(cfg, engine=engine) as owned:
        return await EnrichmentRunner(owned, settings=cfg).run(
            limit=limit,
            source=source,
            lead_id=lead_id,
            force=force,
            resolve_only=resolve_only,
            deep_resolve=deep_resolve,
        )


# ------------------------------------------------------------------ helpers
def _result_for_resolution(
    resolved: ResolvedDomain | None,
    *,
    status: EnrichmentStatusDB = EnrichmentStatusDB.NO_SITE,
    reason: str | None = None,
    evidence: dict | None = None,
) -> EnrichmentResult:
    return EnrichmentResult(
        status=status,
        domain=resolved.domain if resolved else None,
        domain_source=resolved.source if resolved else "none",
        site_url=resolved.site_url if resolved else None,
        evidence=evidence or {},
        reason=reason or ("domain_unresolved" if resolved is None else None),
    )


def _failure_result(resolved: ResolvedDomain, snapshot: PageSnapshot) -> EnrichmentResult:
    if snapshot.error == "robots_disallowed":
        status, reason = EnrichmentStatusDB.NO_SITE, "robots_disallowed"
    elif snapshot.error == "deadline":
        status, reason = EnrichmentStatusDB.ERROR, "deadline"
    elif is_challenge_page(snapshot) or snapshot.status in _BLOCK_STATUS:
        status, reason = EnrichmentStatusDB.BLOCKED, f"blocked:{snapshot.status or snapshot.error}"
    elif snapshot.status in (404, 410):
        status, reason = EnrichmentStatusDB.NO_SITE, f"http_{snapshot.status}"
    elif snapshot.status is not None and snapshot.status >= 500:
        status, reason = EnrichmentStatusDB.ERROR, f"http_{snapshot.status}"
    elif snapshot.error and (snapshot.error == "timeout" or snapshot.error.startswith("network:")):
        status, reason = EnrichmentStatusDB.ERROR, snapshot.error
    else:
        status, reason = EnrichmentStatusDB.NO_SITE, snapshot.error or "no_site"

    return EnrichmentResult(
        status=status,
        domain=resolved.domain,
        domain_source=resolved.source,
        site_url=resolved.site_url,
        evidence={**resolved.evidence, "failure": snapshot.error, "status": snapshot.status},
        reason=reason,
        last_error=snapshot.error,
        engine=snapshot.engine,
    )


def _page_meta(snapshot: PageSnapshot, *, text_chars: int | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "url": snapshot.final_url,
        "status": snapshot.status,
        "engine": snapshot.engine,
        "bytes": snapshot.bytes,
        "error": snapshot.error,
    }
    if text_chars is not None:
        meta["text_chars"] = text_chars
    return meta


def _product_sample(lead: Any) -> list[Any]:
    raw = getattr(lead, "raw", None) or {}
    products = raw.get("products") if isinstance(raw, dict) else None
    return products[:12] if isinstance(products, list) else []


def _engine_label(engines: set[str]) -> str:
    if not engines:
        return "http"
    if len(engines) == 1:
        return next(iter(engines))
    return "mixed"
