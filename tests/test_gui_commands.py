"""The GUI's command catalogue must stay honest about the real CLI.

These tests are deliberately Qt-free: they import only the declarative specs,
so a broken PyQt install cannot hide a drifted flag. The catalogue is the GUI's
single source of truth, and the last test here walks the actual Typer app to
prove every spec still resolves to a command that exists.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.gui import commands as gui
from forgesmith.gui.payload import (
    cell_text,
    doctor_headline,
    doctor_rows,
    human_bytes,
    parse_payload,
    rows_of,
    run_rows,
    status_kpis,
    status_strip,
    window_text,
)


# ------------------------------------------------------------------ catalogue
def _spec_ids() -> list[str]:
    return [spec.id for spec in gui.COMMANDS]


def test_command_ids_are_unique() -> None:
    ids = _spec_ids()
    assert len(ids) == len(set(ids))


def test_every_spec_has_a_summary_and_label() -> None:
    for spec in gui.COMMANDS:
        assert spec.label, spec.id
        assert spec.summary, spec.id


def test_every_spec_resolves_to_a_real_cli_command() -> None:
    """Guard against a renamed CLI command leaving a dead button in the GUI."""
    groups = {group.name: group for group in cli.app.registered_groups}
    top_level = {command.name for command in cli.app.registered_commands}

    for spec in gui.COMMANDS:
        head = spec.argv[0]
        if len(spec.argv) == 1:
            assert head in top_level, f"{spec.id}: unknown command {head}"
            continue
        group = groups.get(head)
        assert group is not None, f"{spec.id}: unknown group {head}"
        sub = {command.name for command in group.typer_instance.registered_commands}
        assert spec.argv[1] in sub, f"{spec.id}: unknown subcommand {spec.argv[0]} {spec.argv[1]}"


def test_every_flag_a_spec_emits_is_accepted_by_the_cli() -> None:
    """Ask Typer for each command's real flags and compare with the catalogue."""
    accepted = _accepted_flags()
    for spec in gui.COMMANDS:
        key = " ".join(spec.argv)
        assert key in accepted, f"{spec.id}: {key} is not a CLI command"
        for field in spec.fields:
            for flag in filter(None, (field.flag, field.negated_flag)):
                if field.kind == "bool" and flag != field.flag:
                    continue
                assert flag in accepted[key], f"{spec.id}: {flag} is not a {key} option"
        if spec.auto_yes:
            assert "--yes" in accepted[key], f"{spec.id}: --yes is not accepted"


def _accepted_flags() -> dict[str, set[str]]:
    """``{"send": {"--limit", ...}, "inbox sync": {...}}`` straight from Typer."""
    accepted: dict[str, set[str]] = {}
    root = typer.main.get_command(cli.app)

    def walk(command: object, prefix: str) -> None:
        children = getattr(command, "commands", None)
        if children:
            for name, child in children.items():
                walk(child, f"{prefix}{name} ")
            return
        accepted[prefix.strip()] = set(_flags(command))

    walk(root, "")
    return accepted


def _flags(command: object) -> list[str]:
    flags: list[str] = []
    for param in command.params:  # type: ignore[attr-defined]
        flags.extend(param.opts)
        flags.extend(param.secondary_opts)
    return flags


# ----------------------------------------------------------------- argv build
def test_empty_form_runs_the_bare_command() -> None:
    assert gui.build_argv(gui.command("doctor"), {}) == ["doctor"]
    assert gui.build_argv(gui.command("db_init"), {}) == ["db", "init"]


def test_declared_defaults_are_sent_explicitly() -> None:
    """The form shows the defaults, so argv should say what the form says."""
    assert gui.build_argv(gui.command("enrich"), {}) == ["enrich", "--engine", "auto"]
    assert gui.build_argv(gui.command("backup"), {}) == [
        "backup",
        "--keep",
        "0",
        "--outbox",
    ]


def test_blank_text_is_skipped_not_passed_through() -> None:
    argv = gui.build_argv(
        gui.command("enrich"), {"limit": None, "source": "", "lead_id": "  ", "engine": None}
    )
    assert argv == ["enrich"]


def test_numbers_are_validated_and_normalised() -> None:
    argv = gui.build_argv(gui.command("send"), {"limit": " 5 ", "max_runtime": "12.5"})
    assert argv == ["send", "--limit", "5", "--max-runtime", "12.5", "--yes"]


def test_out_of_range_numbers_are_reported() -> None:
    with pytest.raises(gui.MissingFields) as excinfo:
        gui.build_argv(gui.command("source_olx"), {"pages": 99})
    assert "max 20" in str(excinfo.value)


def test_non_numeric_input_is_reported() -> None:
    with pytest.raises(gui.MissingFields) as excinfo:
        gui.build_argv(gui.command("send"), {"limit": "lots"})
    assert "not a number" in str(excinfo.value)


def test_required_field_missing_raises() -> None:
    with pytest.raises(gui.MissingFields) as excinfo:
        gui.build_argv(gui.command("mark"), {})
    assert "Status" in excinfo.value.labels
    assert "Lead id or Send id" in excinfo.value.labels


def test_a_target_is_required_for_commands_that_need_one() -> None:
    """Otherwise the modal fires and the CLI refuses with exit 2."""
    cases = {
        "mark": {"status": "replied"},
        "suppress": {"reason": "opt-out"},
        "unsuppress": {"reason": "mistake"},
        "approve": {"message_id": 4},
        "inbox_apply": {"limit": 10},
    }
    for spec_id, values in cases.items():
        with pytest.raises(gui.MissingFields) as excinfo:
            gui.build_argv(gui.command(spec_id), values)
        assert gui.command(spec_id).requires_one_of_label in excinfo.value.labels, spec_id


def test_one_supplied_target_satisfies_the_rule() -> None:
    assert gui.build_argv(gui.command("mark"), {"send_id": 3, "status": "sent"}) == [
        "mark",
        "--send-id",
        "3",
        "--status",
        "sent",
        "--yes",
    ]
    assert gui.build_argv(gui.command("approve"), {"all_composed": True}) == [
        "approve",
        "--all-composed",
        "--yes",
    ]


def test_multi_value_field_repeats_the_flag() -> None:
    argv = gui.build_argv(gui.command("source_olx"), {"query": ["meble", "sofy"]})
    assert argv[:5] == ["source", "olx", "--query", "meble", "--query"]


def test_boolean_flags_are_emitted_only_when_true() -> None:
    assert gui.build_argv(gui.command("enrich"), {"force": True}) == [
        "enrich",
        "--engine",
        "auto",
        "--force",
    ]
    assert gui.build_argv(gui.command("enrich"), {"force": False}) == ["enrich", "--engine", "auto"]


def test_negated_flag_is_emitted_when_a_default_true_box_is_cleared() -> None:
    argv = gui.build_argv(gui.command("backup"), {"with_outbox": False})
    assert argv == ["backup", "--keep", "0", "--no-outbox"]
    assert gui.build_argv(gui.command("backup"), {"with_outbox": True}) == [
        "backup",
        "--keep",
        "0",
        "--outbox",
    ]


def test_auto_yes_appends_the_human_confirmation_flag() -> None:
    assert gui.build_argv(gui.command("approve"), {"lead_id": 3}) == [
        "approve",
        "--lead-id",
        "3",
        "--yes",
    ]
    assert "--yes" not in gui.build_argv(gui.command("enrich"), {})


# --------------------------------------------------------------- confirmation
def test_safe_commands_never_prompt() -> None:
    for spec_id in ("enrich", "compose", "db_init", "backup", "doctor", "stats"):
        assert gui.confirmation(gui.command(spec_id), {}) is None, spec_id


def test_dangerous_commands_prompt_with_the_cli_wording() -> None:
    prompt = gui.confirmation(gui.command("approve"), {"lead_id": 1})
    assert prompt and "queued" in prompt


def test_live_send_only_prompts_when_live_is_ticked() -> None:
    assert gui.confirmation(gui.command("send"), {"live": False}) is None
    live = gui.confirmation(gui.command("send"), {"live": True})
    assert live and "LIVE SEND" in live


def test_confirmation_tolerates_unknown_placeholders() -> None:
    spec = gui.Command(id="x", label="x", argv=("x",), danger="delete {lead_id}? {missing}")
    assert gui.confirmation(spec, {"lead_id": 7}) == "delete 7? "


# ---------------------------------------------------------------------- tones
@pytest.mark.parametrize(
    ("value", "tone"),
    [
        ("ok", "ok"),
        ("sent", "ok"),
        ("replied", "ok"),
        ("pending", "warn"),
        ("needs_review", "warn"),
        ("failed", "bad"),
        ("error", "bad"),
        ("new", "info"),
        ("queued", "warn"),
        ("skipped", "muted"),
        ("", "muted"),
        (None, "muted"),
    ],
)
def test_status_tone(value: object, tone: str) -> None:
    assert gui.status_tone(value) == tone


@pytest.mark.parametrize(
    ("code", "label", "tone"),
    [
        (0, "success", "ok"),
        (2, "refused", "warn"),
        (3, "warnings", "warn"),
        (75, "busy (skipped)", "warn"),
        (1, "failed", "bad"),
        (130, "failed", "bad"),
    ],
)
def test_exit_tone(code: int, label: str, tone: str) -> None:
    assert gui.exit_tone(code) == (label, tone)


def test_json_commands_use_the_documented_flags() -> None:
    assert gui.JSON_COMMANDS["inbound"] == ("inbox", "list", "--json")
    for key, argv in gui.JSON_COMMANDS.items():
        assert argv[-1] == "--json", key


# -------------------------------------------------------------------- payloads
def test_parse_payload_ignores_leading_noise() -> None:
    assert parse_payload('warning: slow\n{"count": 1}') == {"count": 1}
    assert parse_payload("") == {}
    assert parse_payload("not json") == {}


def test_rows_of_keeps_only_objects() -> None:
    payload = {"rows": [{"id": 1}, "junk", None, {"id": 2}]}
    assert rows_of(payload) == [{"id": 1}, {"id": 2}]
    assert rows_of({}) == []


def test_status_kpis_summarise_the_pipeline() -> None:
    payload = {
        "leads": {"total": 7, "by_status": {"new": 5, "queued": 2}, "by_source": {"olx": 7}},
        "sends": {"sent": 1},
        "uncertain": 0,
        "kill_switch": "armed",
        "outbox": {"files": 2, "bytes": 2048},
        "approval_channel": "interactive",
        "window": {"open": False, "next_opening": "2026-10-26T08:00:00+00:00", "daily_cap": 20},
    }
    cards = {kpi.label: kpi for kpi in status_kpis(payload)}
    assert cards["leads"].value == "7"
    assert cards["leads"].hint == "olx 7"
    assert cards["awaiting review"].value == "0"
    assert cards["queued"].value == "2"
    assert cards["queued"].tone == "warn"
    assert cards["kill switch"].tone == "warn"
    assert cards["window"].value == "closed · 26 Oct 08:00 UTC"
    assert cards["window"].hint == "cap 20/day"
    assert cards["outbox"].value == "2 files · 2.0 KB"


def test_status_kpis_without_data_says_so() -> None:
    cards = status_kpis({})
    assert cards[0].value == "no data"


def test_status_strip_lists_every_status_and_the_approval_channel() -> None:
    strip = status_strip(
        {
            "leads": {"by_status": {"new": 5, "queued": 2}},
            "approval_channel": "interactive",
        }
    )
    assert "new 5" in strip and "queued 2" in strip
    assert "approval: interactive" in strip


def test_window_text_handles_a_broken_policy() -> None:
    assert window_text({"open": None, "error": "bad tz"}).startswith("unavailable")
    assert window_text({}) == "unknown"
    assert window_text({"open": True}) == "open"


def test_run_rows_sort_newest_first_and_drop_junk() -> None:
    payload = {
        "runs": {
            "enrich": {"at": "2026-10-25T10:00:00+00:00", "exit_code": 0, "duration_s": 1.5},
            "send": {"at": "2026-10-25T12:00:00+00:00", "exit_code": 3, "duration_s": 2.0},
            "bad": "junk",
        }
    }
    rows = run_rows(payload)
    assert [row["stage"] for row in rows] == ["send", "enrich"]
    assert rows[0]["stop_reason"] == "-"


def test_doctor_headline_counts_failures_then_warnings() -> None:
    failing = doctor_headline(
        {"checks": [{"status": "fail"}, {"status": "warn"}, {"status": "ok"}]}
    )
    assert failing.value == "1 failing" and failing.tone == "bad"
    warning = doctor_headline({"checks": [{"status": "warn"}, {"status": "ok"}]})
    assert warning.value == "1 warning(s)" and warning.tone == "warn"
    assert doctor_headline({"checks": [{"status": "ok"}]}).tone == "ok"
    assert doctor_headline({}).value == "not run"
    assert doctor_rows({"checks": [{"check": "db"}]}) == [{"check": "db"}]


def test_cell_text_formats_iso_timestamps_and_floats() -> None:
    assert cell_text("2026-10-25T01:43:12.123456+00:00") == "2026-10-25 01:43"
    assert cell_text("2026-10-25 01:43:12.123456") == "2026-10-25 01:43"
    assert cell_text(0.5) == "0.50"
    assert cell_text(True) == "yes"
    assert cell_text(None) == "-"
    assert cell_text("") == "-"
    assert cell_text("plain") == "plain"


def test_human_bytes_rounds_sensibly() -> None:
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(None) == "0 B"
    assert human_bytes("junk") == "-"


# --------------------------------------------------------------- spec sanity
def test_required_fields_are_not_advanced_only() -> None:
    """A required field must be visible, or the GUI cannot satisfy it."""
    for spec in gui.COMMANDS:
        for field in spec.fields:
            if field.required:
                assert not field.advanced, f"{spec.id}.{field.key}"


def test_choice_fields_always_offer_choices() -> None:
    for spec in gui.COMMANDS:
        for field in spec.fields:
            if field.kind == "choice":
                assert field.choices, f"{spec.id}.{field.key}"
                if field.default is not None:
                    assert field.default in field.choices


def test_boolean_defaults_match_the_cli_defaults() -> None:
    backup = {field.key: field for field in gui.command("backup").fields}
    assert backup["with_outbox"].default is True
    send = {field.key: field for field in gui.command("send").fields}
    assert send["live"].default is None or send["live"].default is False


def test_cli_json_flag_still_exists_on_every_json_command() -> None:
    """The GUI parses these payloads, so the flags must stay machine-readable."""
    runner = CliRunner()
    for key, argv in gui.JSON_COMMANDS.items():
        result = runner.invoke(cli.app, [*argv, "--help"])
        assert result.exit_code == 0, key
        assert "--json" in result.output, key
