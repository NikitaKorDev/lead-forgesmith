"""Smoke tests for the Qt layer: every command is reachable, nothing crashes.

PyQt5 aborts the whole process on an unhandled exception inside a slot (exit
``0xC0000409``), so these tests build the real window with the runner stubbed
out and assert on structure, not on pixels.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtCore import QByteArray
from PyQt5.QtWidgets import QApplication

from forgesmith.gui import app as gui_app
from forgesmith.gui import commands as gui
from forgesmith.gui.app import NAV_PAGES, STYLESHEET, MainWindow, Step
from forgesmith.gui.runner import CliRunner, Result
from forgesmith.gui.widgets import CommandCard

#: Commands with no form of their own; pages call them from a selection.
BUTTON_ONLY = {"approve", "unapprove", "inbox_show", "inbox_apply"}


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication(["pytest"])
    instance.setStyleSheet(STYLESHEET)
    yield instance


class _StubWindow(MainWindow):
    """A window whose runs never start a process."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.chains: list[list[Step]] = []
        super().__init__()

    def run_chain(self, steps, *, owner=None):  # type: ignore[override]
        steps = list(steps)
        self.chains.append(steps)
        self.calls.extend((list(step.argv), step.json_flag) for step in steps)
        note = getattr(owner, "note_loaded", None)
        if callable(note):
            note()
        return True

    def run_stage(self, argv, *, json_flag=False, owner=None, followup=None):  # type: ignore[override]
        return self.run_chain(
            [Step(list(argv), json_flag=json_flag, followup=followup)], owner=owner
        )


@pytest.fixture()
def window(app: QApplication) -> _StubWindow:
    win = _StubWindow()
    yield win
    win.close()
    win.deleteLater()


def test_every_page_is_built(window: _StubWindow) -> None:
    assert [page_id for page_id, _label in NAV_PAGES] == list(window.pages)
    assert window.stack.count() == len(NAV_PAGES)
    assert len(window.strip.buttons) == 6
    assert window.strip.counts() == {
        "collect": 0,
        "enrich": 0,
        "draft": 0,
        "approve": 0,
        "send": 0,
        "replies": 0,
    }


def test_every_command_is_reachable_from_the_ui(window: _StubWindow) -> None:
    cards = {
        card._spec.id for page in window.pages.values() for card in page.findChildren(CommandCard)
    }
    assert cards | BUTTON_ONLY == set(gui.COMMANDS_BY_ID)
    assert not (cards & BUTTON_ONLY)


def test_navigation_keeps_working_while_a_stage_runs(window: _StubWindow) -> None:
    window._set_busy(True)
    assert window.strip.isEnabled()
    assert window.stack.isEnabled()
    assert window.stop_button.isEnabled()

    window._set_busy(False)
    assert not window.stop_button.isEnabled()


def test_a_status_payload_lands_on_the_pipeline_and_the_strip(window: _StubWindow) -> None:
    payload = {
        "database": "sqlite+aiosqlite:///x.db",
        "leads": {"total": 3, "by_status": {"queued": 3}, "by_source": {"olx": 3}},
        "kill_switch": "armed",
        "outbox": {"files": 0, "bytes": 0},
        "funnel": {
            "collected": 3,
            "with_domain": 2,
            "domainless": 1,
            "enrichable": 1,
            "composable": 1,
            "drafted": 0,
            "approved": 1,
            "parked": 0,
            "done": 0,
        },
        "inbox": {},
        "blockers": {},
    }
    window._status = payload
    window._on_finished(
        Result(
            argv=["status", "--json"],
            exit_code=0,
            duration_s=0.5,
            stdout="",
            stderr="",
            payload=payload,
        )
    )

    assert window.strip.counts()["enrich"] == 1
    assert window.strip.counts()["send"] == 1
    # Work in flight finishes first: an approved message outranks a new draft.
    assert window.pages["pipeline"].next_card.title.text() == "Send 1 approved message"


def test_navigating_with_values_fills_the_target_form(window: _StubWindow) -> None:
    window.navigate("draft", {"lead_id": 42})
    assert window.stack.currentWidget() is window.pages["draft"]
    assert window.pages["draft"].form.card.form.values()["lead_id"] == "42"


def test_json_runs_buffer_stdout_instead_of_dumping_it(window: _StubWindow, monkeypatch) -> None:
    runner = CliRunner()
    chunks: list[tuple[str, str]] = []
    runner.chunk.connect(lambda text, channel: chunks.append((text, channel)))
    monkeypatch.setattr(
        runner._process,
        "readAllStandardOutput",
        lambda: QByteArray(b'{"count": 1, "rows": []}'),
    )

    runner._json = True
    runner._read_stdout()
    assert chunks == [], "raw JSON must not be streamed into the console"
    assert "".join(runner._stdout).startswith('{"count"')

    runner._json = False
    runner._read_stdout()
    assert chunks and chunks[0][1] == "out"


def test_form_round_trips_values_through_the_spec() -> None:
    card = CommandCard(gui.command("enrich"))
    # Deep resolve is the GUI's default: a lead with no guessed shop subdomain
    # would otherwise be parked after a single attempt.
    assert card.form.values()["deep_resolve"] is True

    card.form.set_value("lead_id", 7)
    card.form.set_value("engine", "browser")
    card.form.set_value("resolve_only", True)

    values = card.form.values()
    assert values["lead_id"] == "7"
    assert values["engine"] == "browser"
    assert values["resolve_only"] is True
    assert gui.build_argv(card._spec, values) == [
        "enrich",
        "--lead-id",
        "7",
        "--engine",
        "browser",
        "--resolve-only",
        "--deep-resolve",
    ]


def test_choice_fields_offer_an_empty_option_unless_required() -> None:
    enrich = CommandCard(gui.command("enrich"))
    mark = CommandCard(gui.command("mark"))

    source = enrich.form._editors["source"]
    assert source.itemData(0) is None  # "any"

    status = mark.form._editors["status"]
    assert status.count() == 5
    assert status.itemData(0) == "replied"  # required: no blank entry


# ---------------------------------------------------------------- bulk actions
def _result(argv: list[str], payload: dict[str, object]) -> Result:
    return Result(
        argv=list(argv),
        exit_code=0,
        duration_s=0.2,
        stdout="",
        stderr="",
        payload=dict(payload),
    )


def test_drafting_one_lead_checks_then_enriches_at_most_once(window: _StubWindow) -> None:
    """The silent `scanned 0` was the bug: a lead id must explain itself first."""
    page = window.pages["draft"]
    page.draft_lead(42)

    step = window.chains[-1][0]
    assert step.argv == ["compose", "--check", "--json", "--lead-id", "42"]
    assert step.json_flag is True

    needs_enrichment = _result(
        step.argv,
        {"leads": [{"lead_id": 42, "next_action": "enrich", "reason": "no domain yet"}]},
    )
    chained = step.followup(needs_enrichment)
    assert [item.argv for item in chained] == [
        ["enrich", "--lead-id", "42", "--deep-resolve"],
        ["compose", "--check", "--json", "--lead-id", "42"],
    ]

    recheck = chained[1]
    ready = _result(recheck.argv, {"leads": [{"lead_id": 42, "next_action": "draft"}]})
    assert [item.argv for item in recheck.followup(ready)] == [["compose", "--lead-id", "42"]]

    # Still not enrichable after one attempt: compose and let the CLI say why.
    still_parked = _result(recheck.argv, {"leads": [{"lead_id": 42, "next_action": "enrich"}]})
    assert [item.argv for item in recheck.followup(still_parked)] == [
        ["compose", "--lead-id", "42"]
    ]


def test_the_draft_form_routes_a_single_lead_through_the_chain(window: _StubWindow) -> None:
    page = window.pages["draft"]

    page._run(gui.command("compose"), ["compose", "--lead-id", "9"])
    assert window.chains[-1][0].argv == ["compose", "--check", "--json", "--lead-id", "9"]

    # The operator's own flags survive the detour through the check.
    page._run(gui.command("compose"), ["compose", "--lead-id", "9", "--regenerate"])
    step = window.chains[-1][0]
    assert step.argv == ["compose", "--check", "--json", "--lead-id", "9", "--regenerate"]
    ready = _result(step.argv, {"leads": [{"lead_id": 9, "next_action": "draft"}]})
    assert [item.argv for item in step.followup(ready)] == [
        ["compose", "--lead-id", "9", "--regenerate"]
    ]

    # A batch run is the plain command: the chain only exists for one lead.
    page._run(gui.command("compose"), ["compose", "--limit", "5"])
    assert window.calls[-1][0] == ["compose", "--limit", "5"]


def test_enrich_bulk_buttons_pass_a_limit_and_keep_deep_resolve(window: _StubWindow) -> None:
    page = window.pages["enrich"]

    page._enrich(25)
    assert window.calls[-1][0] == ["enrich", "--deep-resolve", "--limit", "25"]

    page._enrich(None)
    assert window.calls[-1][0] == ["enrich", "--deep-resolve"]


def test_retry_parked_forces_only_the_parked_leads(window: _StubWindow) -> None:
    """--force on its own would re-crawl the whole enriched backlog."""
    page = window.pages["enrich"]
    page._rows = [
        {"id": 3, "next_action": "retry"},
        {"id": 4, "next_action": "enrich"},
        {"id": 5, "next_action": "retry"},
    ]

    page._retry_parked()

    assert [step.argv for step in window.chains[-1]] == [
        ["enrich", "--lead-id", "3", "--deep-resolve", "--force"],
        ["enrich", "--lead-id", "5", "--deep-resolve", "--force"],
    ]


def test_approve_all_lists_what_it_will_approve(
    window: _StubWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake_confirm(_parent: object, _title: str, text: str) -> bool:
        prompts.append(text)
        return True

    monkeypatch.setattr(gui_app, "confirm_dialog", fake_confirm)
    page = window.pages["approve"]
    page._rows = [
        {
            "lead_id": 1,
            "message_id": 10,
            "lead_status": "composed",
            "company": "a.pl",
            "contact": "x@a.pl",
            "subject": "Wspolpraca",
        },
        {
            "lead_id": 2,
            "message_id": 11,
            "lead_status": "queued",
            "company": "b.pl",
            "contact": "y@b.pl",
            "subject": "Already approved",
        },
    ]

    page._approve_all()

    assert "x@a.pl" in prompts[0] and "y@b.pl" not in prompts[0]
    assert window.calls[-1][0] == ["approve", "--all-composed", "--yes", "--limit", "1"]


def test_send_live_shows_recipients_and_dry_run_never_goes_live(
    window: _StubWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake_confirm(_parent: object, _title: str, text: str) -> bool:
        prompts.append(text)
        return True

    monkeypatch.setattr(gui_app, "confirm_dialog", fake_confirm)
    page = window.pages["send"]
    page._drafts = [
        {"lead_id": 5, "company": "c.pl", "contact": "z@c.pl", "lead_status": "queued"},
        {"lead_id": 6, "company": "d.pl", "contact": "w@d.pl", "lead_status": "composed"},
    ]
    page.batch.setValue(3)

    page._dry_run()
    assert "--live" not in window.calls[-1][0]

    page._send_live()
    assert "z@c.pl" in prompts[0] and "w@d.pl" not in prompts[0]
    assert window.calls[-1][0] == ["send", "--live", "--yes", "--limit", "3"]


def test_send_live_is_skipped_when_the_operator_cancels(
    window: _StubWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gui_app, "confirm_dialog", lambda *_args: False)
    page = window.pages["send"]
    before = len(window.calls)

    page._send_live()

    assert len(window.calls) == before


# ------------------------------------------------------------ finding things
def test_the_collect_search_narrows_the_table_without_a_run(window: _StubWindow) -> None:
    page = window.pages["collect"]
    page._rows = [
        {"id": 1, "name": "Meblex", "domain": "meblex.pl", "city": "Gdańsk"},
        {"id": 2, "name": "Inne", "domain": "inne.com", "city": "Kraków"},
    ]
    page._total = 2
    before = len(window.calls)

    page.search.setText("krak")

    assert page.table.table.rowCount() == 1
    assert len(window.calls) == before, "searching is client-side, not a CLI run"
    page.search.setText("")
    assert page.table.table.rowCount() == 2


def test_a_review_row_opens_the_approve_page_on_that_lead(window: _StubWindow) -> None:
    page = window.pages["collect"]

    page.lead_action({"id": 38, "next_action": "review", "reason": ""})

    assert window.stack.currentWidget() is window.pages["approve"]
    assert window.pages["approve"].search.text() == "38"


def test_open_lead_jumps_to_collect_with_the_search_filled(window: _StubWindow) -> None:
    page = window.pages["approve"]
    page._rows = [
        {
            "lead_id": 38,
            "message_id": 6,
            "company": "regama.pl",
            "contact": "biuro@regama.pl",
            "subject": "Wspolpraca",
            "lead_status": "composed",
        }
    ]
    page._fill()

    page._open_lead()

    assert window.stack.currentWidget() is window.pages["collect"]
    assert window.pages["collect"].search.text() == "regama.pl"


def test_ctrl_enter_runs_the_current_pages_form(window: _StubWindow) -> None:
    window.navigate("enrich")

    window._run_current_form()

    assert window.calls[-1][0][0] == "enrich"
    assert "--deep-resolve" in window.calls[-1][0]
