"""Sequential compose runner: clean, prompt, generate, validate, persist."""

from __future__ import annotations

import time

import structlog

from forgesmith.compose.context import (
    build_fact_payload,
    context_hash,
    payload_is_insufficient,
)
from forgesmith.compose.llm import LLMClient, OllamaClient
from forgesmith.compose.models import (
    ComposeError,
    ComposeOutcome,
    ComposeStats,
    DraftParseError,
    DraftPayload,
    FactPayload,
    LLMError,
    ValidationResult,
)
from forgesmith.compose.prompt import (
    FOOTER_VERSION,
    PROMPT_VERSION,
    SenderProfile,
    assemble_body,
    build_footer,
    build_prompt,
    build_repair_prompt,
)
from forgesmith.compose.validate import parse_draft, validate_draft
from forgesmith.config import Settings
from forgesmith.config import settings as default_settings
from forgesmith.db import EnrichmentRepo, LeadRepo, init_db, session_scope
from forgesmith.db.models import Contact, Lead
from forgesmith.db.repo import ComposeRepo, SuppressionSet
from forgesmith.domain import same_site

log = structlog.get_logger(__name__)


class ComposeRunner:
    def __init__(
        self,
        llm: LLMClient,
        *,
        settings: Settings | None = None,
        sender: SenderProfile | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or default_settings
        self.sender = sender

    async def run(
        self,
        *,
        limit: int | None = None,
        source: str | None = None,
        lead_id: int | None = None,
        regenerate: bool = False,
        retry_failed: bool = False,
    ) -> ComposeStats:
        sender = self.sender or SenderProfile.from_settings(self.settings)
        stats = ComposeStats()
        started = time.monotonic()

        async with session_scope(self.settings) as session:
            leads = list(
                await LeadRepo(session).select_composable(
                    limit=limit,
                    source=source,
                    lead_id=lead_id,
                    regenerate=regenerate,
                    retry_failed=retry_failed,
                    max_attempts=self.settings.compose_max_attempts,
                )
            )
            suppressions = await EnrichmentRepo(session).load_suppressions()

        for lead in leads:
            stats.scanned += 1
            try:
                await self._process(
                    lead,
                    sender=sender,
                    suppressions=suppressions,
                    stats=stats,
                )
            except Exception as exc:  # one lead must not abort the run
                log.warning("compose.lead_failed", lead_id=lead.id)
                await self._record_failure(
                    lead.id,
                    reason=f"exception:{type(exc).__name__}",
                    errors=[],
                )
                stats.failed += 1
                stats.outcomes.append(
                    ComposeOutcome(lead.id, "failed", f"exception:{type(exc).__name__}")
                )

        stats.duration_s = round(time.monotonic() - started, 2)
        return stats

    async def _process(
        self,
        lead: Lead,
        *,
        sender: SenderProfile,
        suppressions: SuppressionSet,
        stats: ComposeStats,
    ) -> None:
        contact = _choose_contact(lead, suppressions)
        if contact is None:
            await self._record_skip(lead.id, "no_role_contact")
            stats.skipped += 1
            stats.outcomes.append(ComposeOutcome(lead.id, "skipped", "no_role_contact"))
            return

        payload = build_fact_payload(
            lead,
            contact.kind,
            min_context_chars=self.settings.compose_min_context_chars,
            max_excerpt_chars=self.settings.enrich_text_chars,
        )
        if payload_is_insufficient(
            payload, min_context_chars=self.settings.compose_min_context_chars
        ):
            await self._record_skip(lead.id, "insufficient_context")
            stats.skipped += 1
            stats.outcomes.append(ComposeOutcome(lead.id, "skipped", "insufficient_context"))
            return

        system, user = build_prompt(payload, sender)
        footer = build_footer(sender)
        ctx_hash = context_hash(payload)
        model = self.settings.ollama_model

        try:
            draft, validation = await self._generate(
                system=system,
                user=user,
                payload=payload,
                footer=footer,
            )
        except LLMError as exc:
            await self._record_failure(
                lead.id,
                reason=str(exc),
                model=model,
                prompt_version=PROMPT_VERSION,
                context_hash=ctx_hash,
            )
            stats.failed += 1
            stats.outcomes.append(ComposeOutcome(lead.id, "failed", str(exc)))
            return
        except _DraftRejected as exc:
            await self._record_failure(
                lead.id,
                reason="validation_failed",
                errors=exc.result.errors,
                model=model,
                prompt_version=PROMPT_VERSION,
                context_hash=ctx_hash,
            )
            stats.failed += 1
            stats.outcomes.append(ComposeOutcome(lead.id, "failed", "validation_failed"))
            return

        try:
            full_body = assemble_body(draft.body, footer)
        except ValueError:
            await self._record_failure(
                lead.id,
                reason="footer_count",
                model=model,
                prompt_version=PROMPT_VERSION,
                context_hash=ctx_hash,
            )
            stats.failed += 1
            stats.outcomes.append(ComposeOutcome(lead.id, "failed", "footer_count"))
            return

        hooks = {
            "context_hash": ctx_hash,
            "evidence": validation.grounded_evidence,
            "validation": validation.as_hooks(),
            "footer_version": FOOTER_VERSION,
        }
        async with session_scope(self.settings) as session:
            await ComposeRepo(session).save_draft(
                lead_id=lead.id,
                contact_id=contact.id,
                subject=draft.subject.strip(),
                body=full_body,
                model=model,
                prompt_version=PROMPT_VERSION,
                hooks=hooks,
            )
        stats.drafted += 1
        stats.outcomes.append(ComposeOutcome(lead.id, "drafted"))

    async def _generate(
        self,
        *,
        system: str,
        user: str,
        payload: FactPayload,
        footer: str,
    ) -> tuple[DraftPayload, ValidationResult]:
        raw = await self.llm.complete(system=system, user=user)
        draft, result, errors = _evaluate(raw, payload, footer, self.settings)
        if draft is not None and result is not None and result.ok:
            return draft, result

        repair_errors = list(errors)
        if result is not None and "body_word_count" in repair_errors:
            repair_errors.append(f"body_word_count={result.word_count} (hard range 80-140)")
        repair_user = build_repair_prompt(user, raw, repair_errors)
        repaired = await self.llm.complete(system=system, user=repair_user)
        draft, result, errors = _evaluate(repaired, payload, footer, self.settings)
        if draft is not None and result is not None and result.ok:
            return draft, result
        raise _DraftRejected(result or ValidationResult(errors=errors))

    async def _record_skip(self, lead_id: int, reason: str) -> None:
        async with session_scope(self.settings) as session:
            await ComposeRepo(session).record_skip(lead_id, reason)

    async def _record_failure(
        self,
        lead_id: int,
        *,
        reason: str,
        errors: list[str] | None = None,
        model: str | None = None,
        prompt_version: str = "",
        context_hash: str | None = None,
    ) -> None:
        async with session_scope(self.settings) as session:
            await ComposeRepo(session).record_failure(
                lead_id,
                reason=reason,
                errors=errors or [],
                model=model,
                prompt_version=prompt_version,
                context_hash=context_hash,
            )


async def compose_pending(
    *,
    limit: int | None = None,
    source: str | None = None,
    lead_id: int | None = None,
    regenerate: bool = False,
    retry_failed: bool = False,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    sender: SenderProfile | None = None,
) -> ComposeStats:
    """Public entry point used by the CLI and tests."""
    cfg = settings or default_settings
    await init_db(cfg)
    if llm is not None:
        return await ComposeRunner(llm, settings=cfg, sender=sender).run(
            limit=limit,
            source=source,
            lead_id=lead_id,
            regenerate=regenerate,
            retry_failed=retry_failed,
        )

    async with OllamaClient.from_settings(cfg) as client:
        return await ComposeRunner(client, settings=cfg, sender=sender).run(
            limit=limit,
            source=source,
            lead_id=lead_id,
            regenerate=regenerate,
            retry_failed=retry_failed,
        )


class _DraftRejected(ComposeError):
    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__(", ".join(result.errors) or "validation failed")


def _evaluate(
    raw: str,
    payload: FactPayload,
    footer: str,
    cfg: Settings,
) -> tuple[DraftPayload | None, ValidationResult | None, list[str]]:
    try:
        draft = parse_draft(raw)
    except DraftParseError as exc:
        return None, None, [str(exc)]
    result = validate_draft(
        draft,
        payload,
        footer,
        min_words=cfg.compose_body_min_words,
        max_words=cfg.compose_body_max_words,
    )
    return draft, result, result.errors


def _choose_contact(lead: Lead, suppressions: SuppressionSet) -> Contact | None:
    if suppressions.blocks_domain(lead.domain):
        return None
    candidates = [
        contact
        for contact in lead.contacts
        if contact.kind == "role" and not suppressions.blocks_email(contact.email)
    ]
    if not candidates:
        return None

    def key(contact: Contact) -> tuple[float, int, str]:
        email_domain = contact.email.rsplit("@", 1)[-1]
        return (
            -float(contact.confidence or 0.0),
            0 if same_site(email_domain, lead.domain or "") else 1,
            contact.email.casefold(),
        )

    return min(candidates, key=key)
