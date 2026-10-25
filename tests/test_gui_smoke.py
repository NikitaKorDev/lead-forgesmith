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

from forgesmith.gui import commands as gui
from forgesmith.gui.app import NAV_PAGES, STYLESHEET, MainWindow
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
        super().__init__()

    def run_stage(self, argv, *, json_flag=False, owner=None):  # type: ignore[override]
        self.calls.append((list(argv), json_flag))
        note = getattr(owner, "note_loaded", None)
        if callable(note):
            note()
        return True


@pytest.fixture()
def window(app: QApplication) -> _StubWindow:
    win = _StubWindow()
    yield win
    win.close()
    win.deleteLater()


def test_every_page_is_built(window: _StubWindow) -> None:
    assert [page_id for page_id, _label in NAV_PAGES] == list(window.pages)
    assert window.stack.count() == len(NAV_PAGES)
    assert window.nav.count() == len(NAV_PAGES)


def test_every_command_is_reachable_from_the_ui(window: _StubWindow) -> None:
    cards = {
        card._spec.id
        for page in window.pages.values()
        for card in page.findChildren(CommandCard)
    }
    assert cards | BUTTON_ONLY == set(gui.COMMANDS_BY_ID)
    assert not (cards & BUTTON_ONLY)


def test_navigation_keeps_working_while_a_stage_runs(window: _StubWindow) -> None:
    window._set_busy(True)
    assert window.nav.isEnabled()
    assert window.stack.isEnabled()
    assert window.stop_button.isEnabled()

    window._set_busy(False)
    assert not window.stop_button.isEnabled()


def test_a_result_lands_on_the_page_that_asked_for_it(window: _StubWindow) -> None:
    dashboard = window.pages["dashboard"]
    result = Result(
        argv=["status", "--json"],
        exit_code=0,
        duration_s=0.5,
        stdout="",
        stderr="",
        payload={
            "leads": {"total": 3, "by_status": {"queued": 3}, "by_source": {"olx": 3}},
            "kill_switch": "armed",
            "outbox": {"files": 0, "bytes": 0},
        },
    )
    window._owner = dashboard
    dashboard.on_result(result)

    assert window.calls[-1] == (["doctor", "--json"], True)


def test_navigating_with_values_fills_the_target_form(window: _StubWindow) -> None:
    window.navigate("compose", {"lead_id": 42})
    assert window.stack.currentWidget() is window.pages["compose"]
    assert window.pages["compose"].card.form.values()["lead_id"] == "42"


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
    ]


def test_choice_fields_offer_an_empty_option_unless_required() -> None:
    enrich = CommandCard(gui.command("enrich"))
    mark = CommandCard(gui.command("mark"))

    source = enrich.form._editors["source"]
    assert source.itemData(0) is None  # "any"

    status = mark.form._editors["status"]
    assert status.count() == 5
    assert status.itemData(0) == "replied"  # required: no blank entry
