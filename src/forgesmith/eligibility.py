"""One answer to "what can happen to this lead next?".

The CLI, the GUI and ``compose --check`` must agree on why a lead is or is not
eligible for a stage. Before this module the rule lived in three places: the
``select_composable`` SQL, the compose runner, and the operator's head. A lead
that silently produced ``scanned 0`` was the visible symptom.

``evaluate`` is pure so it can be tested without a database; the repo supplies
the handful of facts it needs (message exists, contacts, suppressions).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forgesmith.db.models import EnrichmentStatusDB, LeadStatusDB

#: States that the pipeline can still move forward, with the button to press.
ACTIONABLE = frozenset({"enrich", "draft", "review", "send", "retry"})


class LeadState(StrEnum):
    """Where a lead sits, in the operator's words rather than the schema's."""

    COLLECTED = "collected"  # new, no domain yet
    READY_TO_ENRICH = "ready_to_enrich"  # new, storefront known
    READY_TO_DRAFT = "ready_to_draft"  # enriched, contact found, no draft
    DRAFTED = "drafted"  # a draft exists, awaiting review
    APPROVED = "approved"  # queued: eligible for the next send
    PARKED = "parked"  # skipped, or nothing usable was found
    DONE = "done"  # sent / replied / bounced / suppressed


#: Human wording for each state, used by tables and the stage strip.
STATE_LABELS: dict[str, str] = {
    LeadState.COLLECTED: "collected",
    LeadState.READY_TO_ENRICH: "ready to enrich",
    LeadState.READY_TO_DRAFT: "ready to draft",
    LeadState.DRAFTED: "drafted",
    LeadState.APPROVED: "approved",
    LeadState.PARKED: "parked",
    LeadState.DONE: "done",
}


@dataclass(frozen=True, slots=True)
class Eligibility:
    """The state, the next action and a sentence explaining it."""

    state: LeadState
    action: str  # "" (terminal) | enrich | draft | review | send | retry
    label: str  # button text
    reason: str  # why this is the next step (or why there is none)

    @property
    def actionable(self) -> bool:
        return self.action in ACTIONABLE


_TERMINAL_REASONS: dict[str, str] = {
    LeadStatusDB.SENT: "delivered — the ledger has the evidence",
    LeadStatusDB.REPLIED: "the business replied",
    LeadStatusDB.BOUNCED: "bounced; suppressed and never retried",
    LeadStatusDB.SUPPRESSED: "opted out; no future send targets it",
}

_PARKED_REASONS: dict[str, str] = {
    EnrichmentStatusDB.NO_SITE: "no storefront found — retry with force to look again",
    EnrichmentStatusDB.NO_CONTACT: "the site had no usable role address",
    EnrichmentStatusDB.BLOCKED: "the site blocked the crawler",
    EnrichmentStatusDB.ERROR: "the crawl failed; retry with force",
}


def evaluate(
    lead: Any,
    *,
    has_message: bool,
    role_contacts: Iterable[str],
    suppressed_emails: set[str],
    suppressed_domains: set[str],
    enrichment_status: str | None = None,
    compose_attempts: int = 0,
    compose_max_attempts: int = 3,
    enrich_attempts: int = 0,
    enrich_max_attempts: int = 3,
    retry_failed: bool = False,
) -> Eligibility:
    """Classify one lead.

    ``role_contacts`` and the suppression sets are compared case-insensitively,
    matching ``select_composable``'s SQL (``func.lower(...)``).

    The attempt caps mirror the two selection queries: a lead at the compose cap
    is only retried with ``--retry-failed``, and a ``new`` lead at the enrich cap
    only with ``--force``. Saying so is the whole point of the module — those
    two states are otherwise invisible "nothing happened" runs.
    """
    status = str(getattr(lead, "status", "") or "")
    domain = str(getattr(lead, "domain", "") or "").strip().lower()

    if status in _TERMINAL_REASONS:
        return Eligibility(LeadState.DONE, "", "Done", _TERMINAL_REASONS[status])

    if status == LeadStatusDB.QUEUED:
        return Eligibility(
            LeadState.APPROVED,
            "send",
            "Approved",
            "approved — the mailer may send it",
        )

    if status == LeadStatusDB.COMPOSED or has_message:
        return Eligibility(
            LeadState.DRAFTED,
            "review",
            "Review",
            "a draft exists and waits for review",
        )

    if status == LeadStatusDB.SKIPPED:
        reason = _PARKED_REASONS.get(str(enrichment_status or ""), "parked by an earlier run")
        return Eligibility(LeadState.PARKED, "retry", "Retry", reason)

    if status == LeadStatusDB.ENRICHED:
        emails = [email.strip().lower() for email in role_contacts if email]
        usable = [email for email in emails if email not in suppressed_emails]
        if enrichment_status != EnrichmentStatusDB.OK:
            return Eligibility(
                LeadState.PARKED,
                "retry",
                "Retry",
                f"enrichment is {enrichment_status or 'incomplete'}, not ok",
            )
        if not emails:
            return Eligibility(
                LeadState.PARKED,
                "retry",
                "Retry",
                "no role address was stored",
            )
        if not usable:
            return Eligibility(
                LeadState.DONE,
                "",
                "Suppressed",
                "every stored address is suppressed",
            )
        if domain and domain in suppressed_domains:
            return Eligibility(
                LeadState.DONE,
                "",
                "Suppressed",
                "the domain is suppressed",
            )
        if compose_attempts >= compose_max_attempts and not retry_failed:
            return Eligibility(
                LeadState.PARKED,
                "retry",
                "Retry",
                f"compose failed {compose_attempts} times — retry with --retry-failed",
            )
        return Eligibility(
            LeadState.READY_TO_DRAFT,
            "draft",
            "Draft",
            f"contact found ({usable[0]})",
        )

    # status == new (or anything unexpected): still in the collect -> enrich step.
    if has_message:
        return Eligibility(LeadState.DRAFTED, "review", "Review", "a draft exists")
    if enrich_attempts >= enrich_max_attempts:
        return Eligibility(
            LeadState.PARKED,
            "retry",
            "Retry",
            f"enrich failed {enrich_attempts} times — retry with --force",
        )
    if domain:
        return Eligibility(
            LeadState.READY_TO_ENRICH,
            "enrich",
            "Enrich",
            "storefront known; contacts not extracted yet",
        )
    return Eligibility(
        LeadState.COLLECTED,
        "enrich",
        "Enrich",
        "no domain yet — deep resolve reads the OLX offer page to find the shop",
    )


def reason_for_skip(
    *,
    lead_id: int,
    found: bool,
    eligibility: Eligibility | None,
    regenerate: bool,
) -> str:
    """Explain a compose no-op in one sentence, for the CLI and the GUI."""
    if not found:
        return f"lead {lead_id} does not exist"
    assert eligibility is not None
    if eligibility.state == LeadState.DRAFTED and not regenerate:
        return f"lead {lead_id} already has a draft — pass --regenerate to replace it"
    if eligibility.state == LeadState.DRAFTED:
        return f"lead {lead_id} was selected for regeneration but no draft was saved"
    if eligibility.state == LeadState.READY_TO_DRAFT:
        return f"lead {lead_id} is eligible; nothing was drafted (see the failure above)"
    if eligibility.state == LeadState.COLLECTED:
        return f"lead {lead_id} has no domain yet — run enrich (deep resolve) first"
    if eligibility.state == LeadState.READY_TO_ENRICH:
        return f"lead {lead_id} is not enriched yet — run enrich first"
    if eligibility.state == LeadState.APPROVED:
        return f"lead {lead_id} is already approved; its draft is queued"
    if eligibility.state == LeadState.PARKED:
        return f"lead {lead_id} is parked: {eligibility.reason}"
    if eligibility.state == LeadState.DONE:
        return f"lead {lead_id} is finished: {eligibility.reason}"
    return f"lead {lead_id} is not eligible to compose"
