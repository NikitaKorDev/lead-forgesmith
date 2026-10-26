"""The eligibility state machine and the CLI surfaces that expose it.

Every assertion here exists because a lead used to disappear between stages
without a word: compose printed ``scanned 0`` with exit 0 and the operator had
no way to learn which rule rejected it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.compose.models import ComposeOutcome, ComposeStats
from forgesmith.compose.prompt import FOOTER_VERSION
from forgesmith.config import settings
from forgesmith.db.models import Contact, Enrichment, Event, Lead, Message, Suppression
from forgesmith.db.repo import LeadRepo
from forgesmith.db.session import session_scope
from forgesmith.eligibility import Eligibility, LeadState, evaluate, reason_for_skip

runner = CliRunner()


def _lead(status: str, domain: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, domain=domain)


def _classify(lead: SimpleNamespace, **kwargs: object) -> Eligibility:
    base: dict[str, object] = {
        "has_message": False,
        "role_contacts": (),
        "suppressed_emails": set(),
        "suppressed_domains": set(),
    }
    base.update(kwargs)
    return evaluate(lead, **base)  # type: ignore[arg-type]


# --------------------------------------------------------------------- states
def test_new_lead_without_a_domain_is_collected_and_points_at_enrich() -> None:
    result = _classify(_lead("new"))

    assert result.state == LeadState.COLLECTED
    assert result.action == "enrich"
    assert "deep resolve" in result.reason
    assert result.actionable


def test_new_lead_with_a_domain_is_ready_to_enrich() -> None:
    result = _classify(_lead("new", "sklep.pl"))

    assert result.state == LeadState.READY_TO_ENRICH
    assert result.action == "enrich"


def test_enriched_lead_with_an_ok_enrichment_and_role_contact_is_ready_to_draft() -> None:
    result = _classify(
        _lead("enriched", "sklep.pl"),
        enrichment_status="ok",
        role_contacts=["biuro@sklep.pl"],
    )

    assert result.state == LeadState.READY_TO_DRAFT
    assert result.action == "draft"
    assert result.label == "Draft"
    assert "biuro@sklep.pl" in result.reason


def test_lead_with_a_message_is_drafted_even_if_the_status_lags() -> None:
    result = _classify(_lead("enriched", "sklep.pl"), has_message=True)

    assert result.state == LeadState.DRAFTED
    assert result.action == "review"


def test_queued_lead_is_approved() -> None:
    result = _classify(_lead("queued", "sklep.pl"), has_message=True)

    assert result.state == LeadState.APPROVED
    assert result.action == "send"


@pytest.mark.parametrize("status", ["sent", "replied", "bounced", "suppressed"])
def test_terminal_statuses_are_done_and_not_actionable(status: str) -> None:
    result = _classify(_lead(status, "sklep.pl"), has_message=True)

    assert result.state == LeadState.DONE
    assert result.action == ""
    assert not result.actionable


def test_skipped_lead_is_parked_with_the_enrichment_reason() -> None:
    result = _classify(_lead("skipped"), enrichment_status="no_site")

    assert result.state == LeadState.PARKED
    assert result.action == "retry"
    assert "retry with force" in result.reason


def test_enriched_lead_without_a_role_contact_is_parked() -> None:
    result = _classify(_lead("enriched", "sklep.pl"), enrichment_status="ok")

    assert result.state == LeadState.PARKED
    assert "no role address" in result.reason


def test_suppressed_role_address_makes_the_lead_done() -> None:
    result = _classify(
        _lead("enriched", "sklep.pl"),
        enrichment_status="ok",
        role_contacts=["biuro@sklep.pl"],
        suppressed_emails={"biuro@sklep.pl"},
    )

    assert result.state == LeadState.DONE
    assert result.label == "Suppressed"


def test_suppressed_domain_makes_the_lead_done() -> None:
    result = _classify(
        _lead("enriched", "sklep.pl"),
        enrichment_status="ok",
        role_contacts=["biuro@sklep.pl"],
        suppressed_domains={"sklep.pl"},
    )

    assert result.state == LeadState.DONE


def test_compose_attempt_cap_parks_the_lead_unless_retry_is_allowed() -> None:
    kwargs = {
        "enrichment_status": "ok",
        "role_contacts": ["biuro@sklep.pl"],
        "compose_attempts": 3,
        "compose_max_attempts": 3,
    }
    capped = _classify(_lead("enriched", "sklep.pl"), **kwargs)
    retried = _classify(_lead("enriched", "sklep.pl"), **kwargs, retry_failed=True)

    assert capped.state == LeadState.PARKED
    assert "--retry-failed" in capped.reason
    assert retried.state == LeadState.READY_TO_DRAFT


def test_enrich_attempt_cap_parks_a_new_lead() -> None:
    result = _classify(
        _lead("new", "sklep.pl"),
        enrich_attempts=3,
        enrich_max_attempts=3,
    )

    assert result.state == LeadState.PARKED
    assert "--force" in result.reason


# ---------------------------------------------------------------- country gate
@pytest.mark.parametrize("status", ["enriched", "queued"])
def test_a_country_refusal_blocks_a_sendable_lead(status: str) -> None:
    result = _classify(
        _lead(status, "sklep.pl"),
        has_message=True,
        enrichment_status="ok",
        role_contacts=["biuro@sklep.pl"],
        country_refusal="DE requires prior consent — cold email is not enabled for it",
    )

    assert result.state == LeadState.BLOCKED
    assert result.action == ""
    assert result.label == "Blocked"
    assert "prior consent" in result.reason
    assert not result.actionable


def test_a_country_refusal_does_not_block_collection() -> None:
    result = _classify(
        _lead("new", "sklep.pl"),
        country_refusal="lead has no country; run `forgesmith db backfill-country`",
    )

    assert result.state == LeadState.READY_TO_ENRICH


def test_reason_for_skip_explains_a_blocked_lead() -> None:
    blocked = _classify(
        _lead("queued", "sklep.pl"),
        has_message=True,
        country_refusal="country US is not in mail_countries",
    )

    assert (
        reason_for_skip(lead_id=7, found=True, eligibility=blocked, regenerate=False)
        == "lead 7 is blocked from outreach: country US is not in mail_countries"
    )


# --------------------------------------------------------------------- reasons
def test_reason_for_skip_names_the_exact_rule() -> None:
    assert (
        reason_for_skip(lead_id=7, found=False, eligibility=None, regenerate=False)
        == "lead 7 does not exist"
    )
    assert (
        reason_for_skip(
            lead_id=7,
            found=True,
            eligibility=_classify(_lead("new")),
            regenerate=False,
        )
        == "lead 7 has no domain yet — run enrich (deep resolve) first"
    )
    assert (
        reason_for_skip(
            lead_id=7,
            found=True,
            eligibility=_classify(_lead("new", "a.pl")),
            regenerate=False,
        )
        == "lead 7 is not enriched yet — run enrich first"
    )
    assert (
        reason_for_skip(
            lead_id=7,
            found=True,
            eligibility=_classify(_lead("enriched", "a.pl"), has_message=True),
            regenerate=False,
        )
        == "lead 7 already has a draft — pass --regenerate to replace it"
    )


# ------------------------------------------------------------------- fixtures
async def _seed() -> dict[str, int]:
    """One lead per interesting state, mirroring the live database's shape."""
    async with session_scope() as session:
        ids: dict[str, int] = {}

        async def add(
            key: str,
            *,
            status: str,
            domain: str | None,
            country: str | None = "GB",
            enrichment: str | None = None,
            contact: str | None = None,
            message: bool = False,
            attempts: int = 0,
            suppression: tuple[str, str] | None = None,
        ) -> int:
            lead = Lead(
                source="olx",
                source_id=key,
                name=key,
                domain=domain,
                country=country,
                status=status,
            )
            session.add(lead)
            await session.flush()
            if enrichment is not None:
                session.add(
                    Enrichment(
                        lead_id=lead.id,
                        status=enrichment,
                        domain=domain,
                        domain_source="lead" if domain else "none",
                        attempts=attempts,
                    )
                )
            contact_row = None
            if contact:
                contact_row = Contact(lead_id=lead.id, email=contact, kind="role")
                session.add(contact_row)
                await session.flush()
            if message:
                session.add(
                    Message(
                        lead_id=lead.id,
                        contact_id=contact_row.id if contact_row else None,
                        subject="Temat",
                        body="Treść",
                        model="qwen2.5:7b",
                        prompt_version="phase3-v1",
                        hooks={"footer_version": FOOTER_VERSION},
                    )
                )
            if suppression:
                session.add(Suppression(value=suppression[0], kind=suppression[1], reason="test"))
            await session.flush()
            ids[key] = lead.id
            return lead.id

        await add("collected", status="new", domain=None)
        await add("enrichable", status="new", domain="enrichable.pl")
        await add(
            "composable",
            status="enriched",
            domain="composable.pl",
            enrichment="ok",
            contact="biuro@composable.pl",
        )
        await add(
            "drafted",
            status="composed",
            domain="drafted.pl",
            enrichment="ok",
            contact="biuro@drafted.pl",
            message=True,
        )
        await add(
            "approved",
            status="queued",
            domain="approved.pl",
            enrichment="ok",
            contact="biuro@approved.pl",
            message=True,
        )
        await add("parked", status="skipped", domain=None, enrichment="no_site")
        await add(
            "suppressed",
            status="enriched",
            domain="suppressed.pl",
            enrichment="ok",
            contact="biuro@suppressed.pl",
            suppression=("biuro@suppressed.pl", "email"),
        )
        await add(
            "capped",
            status="enriched",
            domain="capped.pl",
            enrichment="ok",
            contact="biuro@capped.pl",
        )
        for _ in range(3):
            session.add(
                Event(
                    entity="lead",
                    entity_id=ids["capped"],
                    action="compose_failed",
                    detail={"reason": "timeout"},
                )
            )
        await session.flush()
        return ids


@pytest.fixture
def seeded(db, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    # The seeded leads are GB; the mailer must be cleared to mail that country.
    monkeypatch.setattr(settings, "mail_countries", "GB")
    return asyncio.run(_seed())


# ------------------------------------------------------------------- repo view
def test_eligibility_map_matches_select_composable(seeded) -> None:
    async def check() -> tuple[set[int], set[int], dict[int, Eligibility]]:
        async with session_scope() as session:
            repo = LeadRepo(session)
            selected = {lead.id for lead in await repo.select_composable()}
            classification = await repo.eligibility_map()
        ready = {
            lead_id
            for lead_id, item in classification.items()
            if item.state == LeadState.READY_TO_DRAFT
        }
        return selected, ready, classification

    selected, ready, classification = asyncio.run(check())

    assert selected == ready == {seeded["composable"]}
    assert classification[seeded["collected"]].state == LeadState.COLLECTED
    assert classification[seeded["parked"]].state == LeadState.PARKED
    assert classification[seeded["capped"]].state == LeadState.PARKED
    assert classification[seeded["approved"]].state == LeadState.APPROVED


def test_eligibility_map_refuses_a_country_the_mailer_would_refuse(seeded) -> None:
    """The state strip must show the same gate the mailer enforces."""

    async def check() -> Eligibility:
        async with session_scope() as session:
            lead = await session.get(Lead, seeded["composable"])
            assert lead is not None
            lead.country = "DE"
        async with session_scope() as session:
            classification = await LeadRepo(session).eligibility_map(
                lead_ids=[seeded["composable"]]
            )
        return classification[seeded["composable"]]

    result = asyncio.run(check())

    assert result.state == LeadState.BLOCKED
    assert result.reason == "DE requires prior consent — cold email is not enabled for it"


def test_funnel_counts_each_stage(seeded) -> None:
    async def read() -> dict[str, int]:
        async with session_scope() as session:
            return await LeadRepo(session).funnel()

    funnel = asyncio.run(read())

    assert funnel["collected"] == 8
    assert funnel["with_domain"] == 6
    assert funnel["domainless"] == 2
    assert funnel["enrichable"] == 2
    assert funnel["composable"] == 1
    assert funnel["drafted"] == 1
    assert funnel["approved"] == 1
    assert funnel["parked"] == 2


# ----------------------------------------------------------------- CLI: check
def test_compose_check_writes_nothing_and_never_calls_the_llm(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(**kwargs: object) -> ComposeStats:  # pragma: no cover - must not run
        raise AssertionError("compose --check must not generate drafts")

    monkeypatch.setattr(cli, "compose_pending", boom)

    result = runner.invoke(cli.app, ["compose", "--check", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["eligible"] == 1
    assert payload["leads"][0]["lead_id"] == seeded["composable"]

    async def read() -> int:
        async with session_scope() as session:
            return len(list((await session.execute(_messages())).scalars()))

    assert asyncio.run(read()) == 2  # the two seeded drafts, none added


def _messages():
    from sqlalchemy import select

    return select(Message)


def test_compose_check_explains_a_single_ineligible_lead(seeded) -> None:
    result = runner.invoke(cli.app, ["compose", "--check", "--lead-id", str(seeded["collected"])])

    assert result.exit_code == 0, result.output
    assert "not eligible" in result.output
    assert "no domain yet" in result.output


def test_compose_check_json_reports_the_reason_for_a_single_lead(seeded) -> None:
    result = runner.invoke(
        cli.app, ["compose", "--check", "--json", "--lead-id", str(seeded["drafted"])]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["eligible"] == 0
    assert payload["leads"][0]["eligible"] is False
    assert payload["leads"][0]["state"] == "drafted"


# ------------------------------------------------------------------ CLI: no-op
def test_compose_without_eligible_leads_prints_the_reason_and_exits_zero(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def empty(**kwargs: object) -> ComposeStats:
        return ComposeStats(duration_s=0.01)

    monkeypatch.setattr(cli, "compose_pending", empty)

    result = runner.invoke(cli.app, ["compose", "--lead-id", str(seeded["collected"])])

    assert result.exit_code == 0, result.output
    assert "Nothing to draft" in result.output
    assert "no domain yet" in result.output


def test_compose_json_carries_per_lead_outcomes(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(**kwargs: object) -> ComposeStats:
        return ComposeStats(
            scanned=1,
            drafted=1,
            duration_s=0.2,
            outcomes=[ComposeOutcome(seeded["composable"], "drafted")],
        )

    monkeypatch.setattr(cli, "compose_pending", fake)

    result = runner.invoke(cli.app, ["compose", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["drafted"] == 1
    assert payload["outcomes"] == [
        {"lead_id": seeded["composable"], "outcome": "drafted", "reason": None}
    ]
    assert payload["reason"] is None


# -------------------------------------------------------------- CLI: status/leads
def test_status_json_exposes_the_funnel_and_blockers(seeded, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_ollama_state", lambda: {"reachable": True, "url": "x", "model": "m"})

    result = runner.invoke(cli.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["funnel"]["collected"] == 8
    assert payload["funnel"]["composable"] == 1
    assert set(payload["blockers"]) == {"kill_switch", "window", "ollama", "sender"}
    assert payload["blockers"]["ollama"]["blocked"] is False
    assert "inbox" in payload


def test_status_marks_ollama_as_a_blocker_when_it_is_down(seeded, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_ollama_state",
        lambda: {"reachable": False, "url": "http://x", "model": "m"},
    )

    result = runner.invoke(cli.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["blockers"]["ollama"]["blocked"] is True
    assert "not listening" in payload["blockers"]["ollama"]["detail"]


def test_leads_json_carries_the_next_action(seeded) -> None:
    result = runner.invoke(cli.app, ["leads", "--limit", "50", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    rows = {row["id"]: row for row in payload["rows"]}
    assert rows[seeded["collected"]]["state"] == "collected"
    assert rows[seeded["collected"]]["next_action"] == "enrich"
    assert rows[seeded["composable"]]["next_action"] == "draft"
    assert rows[seeded["approved"]]["next_action"] == "send"
    assert rows[seeded["parked"]]["next_action"] == "retry"


# ------------------------------------------------------------ CLI: other --json
def test_enrich_json_reports_the_counters(seeded, monkeypatch) -> None:
    from forgesmith.enrichment.models import RunStats

    async def fake(**kwargs: object) -> RunStats:
        return RunStats(scanned=2, enriched=1, no_site=1, duration_s=0.3)

    monkeypatch.setattr(cli, "enrich_pending", fake)

    result = runner.invoke(cli.app, ["enrich", "--json", "--limit", "2"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["scanned"] == 2
    assert payload["enriched"] == 1
    assert payload["no_site"] == 1


def test_approve_json_reports_one_lead(seeded, mail_env) -> None:
    result = runner.invoke(cli.app, ["approve", "--lead-id", str(seeded["drafted"]), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["lead_id"] == seeded["drafted"]
    assert payload["approved"] is True
    assert payload["approved_count"] == 1


def test_approve_json_bulk_lists_the_refusals(seeded, mail_env) -> None:
    result = runner.invoke(cli.app, ["approve", "--all-composed", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["approved_count"] == 1
    assert payload["preview"][0]["lead_id"] == seeded["drafted"]
