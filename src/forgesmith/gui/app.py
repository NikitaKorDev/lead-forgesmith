"""Forgesmith desktop console: a stage-strip pipeline over the real CLI.

The GUI owns no pipeline logic. Every button builds argv, starts one
``python -m forgesmith`` subprocess and shows its output, so approvals, the
kill switch, dry-run gating, locks and the run ledger behave exactly as they do
on the command line. What this window adds is *orientation*: one clickable
stage strip with live counts, one "next step" card, and the blockers that would
otherwise turn a run into a silent no-op.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from PyQt5.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QDesktopServices, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from forgesmith import __version__
from forgesmith.gui.commands import (
    LEAD_STATUSES,
    SOURCES,
    Command,
    MissingFields,
    build_argv,
    command,
    confirmation,
    exit_tone,
)
from forgesmith.gui.payload import (
    STAGES,
    Kpi,
    action_argv,
    action_page,
    blockers_for,
    cell_text,
    doctor_headline,
    doctor_rows,
    funnel_line,
    next_step,
    rows_of,
    rows_where_action,
    run_rows,
    stage_counts,
    status_kpis,
    window_text,
)
from forgesmith.gui.runner import CliRunner, Result
from forgesmith.gui.widgets import (
    BlockerBanner,
    CommandCard,
    ConsolePane,
    KpiCard,
    MarkDialog,
    NextStepCard,
    StageStrip,
    TablePane,
    muted,
    repolish,
    tone_color,
)

STYLESHEET = """
QWidget {
    background: #f4f6fa;
    color: #1f2430;
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 10pt;
}
QLabel { background: transparent; }
QMainWindow::separator { background: #e2e6ee; width: 1px; height: 1px; }
QWidget#Header { background: #ffffff; border-bottom: 1px solid #e2e6ee; }
QWidget#StageBar { background: #ffffff; border-bottom: 1px solid #e2e6ee; }
QLabel#AppName { font-size: 13pt; font-weight: 700; color: #111827; }
QLabel#AppTag { color: #6b7280; font-size: 9pt; }
QLabel#PageTitle { font-size: 15pt; font-weight: 700; color: #111827; }
QLabel#Hint { color: #6b7280; font-size: 9pt; }
QLabel#KpiLabel { color: #6b7280; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }
QLabel#KpiValue { font-size: 14pt; font-weight: 700; }
QLabel#KpiHint { color: #9aa3b2; font-size: 8pt; }
QLabel#Placeholder { color: #9aa3b2; padding: 24px; }
QLabel#Error { color: #b91c1c; font-size: 9pt; }
QLabel#Notice {
    background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px;
    color: #92400e; padding: 8px; font-size: 9pt;
}
QLabel#DangerNotice {
    background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px;
    color: #991b1b; padding: 8px; font-size: 9pt; font-weight: 600;
}
QFrame#Kpi {
    background: #ffffff; border: 1px solid #e2e6ee; border-radius: 10px;
}
QGroupBox {
    background: #ffffff; border: 1px solid #e2e6ee; border-radius: 10px;
    margin-top: 16px; padding: 14px 12px 12px 12px; font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 12px; padding: 0 4px; color: #344054;
}
QFrame#StageStrip { background: transparent; }
QPushButton#Stage {
    background: #ffffff; border: 1px solid #e2e6ee; border-radius: 9px;
    padding: 7px 12px; text-align: left; color: #6b7280; font-size: 9pt;
}
QPushButton#Stage:hover { border-color: #93b4f8; background: #f7f9fd; }
QPushButton#Stage[tone="waiting"] {
    border-color: #bfd3fb; background: #f4f8ff; color: #1d4ed8; font-weight: 600;
}
QPushButton#Stage[active="true"] {
    border: 2px solid #2563eb; background: #ffffff; color: #1d4ed8; font-weight: 700;
}
QPushButton#StageTab {
    background: #ffffff; border: 1px solid #e2e6ee; border-radius: 9px;
    padding: 9px 14px; color: #475467;
}
QPushButton#StageTab:hover { border-color: #93b4f8; background: #f7f9fd; }
QPushButton#StageTab[active="true"] {
    border: 2px solid #2563eb; color: #1d4ed8; font-weight: 700;
}
QFrame#NextStep {
    background: #ffffff; border: 1px solid #e2e6ee; border-left: 4px solid #2563eb;
    border-radius: 10px;
}
QFrame#NextStep[tone="warn"] { border-left-color: #b45309; }
QFrame#NextStep[tone="ok"] { border-left-color: #15803d; }
QFrame#NextStep[tone="muted"] { border-left-color: #9aa3b2; }
QLabel#NextStepCaption { color: #6b7280; font-size: 8pt; font-weight: 700; letter-spacing: 1px; }
QLabel#NextStepTitle { font-size: 14pt; font-weight: 700; color: #111827; }
QFrame#BlockerBanner {
    background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px;
}
QLabel#BlockerHeading { color: #92400e; font-weight: 700; font-size: 9pt; }
QLabel#BlockerText { color: #92400e; font-size: 9pt; }
QLineEdit, QComboBox, QSpinBox {
    background: #ffffff; border: 1px solid #d5dae3; border-radius: 6px;
    padding: 5px 8px; min-height: 18px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #2563eb; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: #f0f2f6; color: #9aa3b2; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 20px; }
QCheckBox { spacing: 8px; color: #344054; background: none; }
QPushButton {
    background: #ffffff; border: 1px solid #d5dae3; border-radius: 6px;
    padding: 6px 14px; color: #1f2430;
}
QPushButton:hover { background: #f1f4f9; }
QPushButton:pressed { background: #e6ebf3; }
QPushButton:disabled { color: #9aa3b2; background: #f0f2f6; }
QPushButton#Primary {
    background: #2563eb; border: none; color: #ffffff; font-weight: 600;
}
QPushButton#Primary:hover { background: #1d4ed8; }
QPushButton#Primary:disabled { background: #b9cdf7; color: #eef2ff; }
QPushButton#Danger { background: #b91c1c; border: none; color: #ffffff; font-weight: 600; }
QPushButton#Danger:hover { background: #991b1b; }
QPushButton#Danger:disabled { background: #eef1f6; color: #9aa3b2; border: 1px solid #e2e6ee; }
QPushButton#Ghost {
    background: transparent; border: none; color: #94a3b8; padding: 3px 8px; font-size: 9pt;
}
QPushButton#Ghost:hover { color: #e2e8f0; background: #1e293b; border-radius: 4px; }
QTableWidget {
    background: #ffffff; border: 1px solid #e2e6ee; border-radius: 8px;
    gridline-color: #eef1f6; selection-background-color: #e8efff;
    selection-color: #1d4ed8; alternate-background-color: #fafbfe;
}
QHeaderView::section {
    background: #f7f9fc; border: none; border-bottom: 1px solid #e2e6ee;
    padding: 6px; font-weight: 600; color: #475467;
}
QTableCornerButton::section { background: #f7f9fc; border: none; }
QTabWidget::pane { border: 1px solid #e2e6ee; border-radius: 8px; background: #ffffff; }
QTabBar::tab {
    background: #eef1f6; border: 1px solid #e2e6ee; border-bottom: none;
    padding: 6px 14px; margin-right: 2px; border-top-left-radius: 6px;
    border-top-right-radius: 6px; color: #475467;
}
QTabBar::tab:selected { background: #ffffff; color: #1d4ed8; font-weight: 600; }
QPlainTextEdit#Console {
    background: #0f172a; color: #e2e8f0; border: none;
    selection-background-color: #334155;
}
QWidget#ConsoleHeader { background: #0b1220; }
QLabel#ConsoleTitle { color: #cbd5e1; font-weight: 700; font-size: 9pt; }
QLabel#ConsoleCommand { color: #64748b; font-size: 9pt; }
QPlainTextEdit#Draft {
    background: #ffffff; border: 1px solid #e2e6ee; border-radius: 8px;
    padding: 10px; font-size: 10pt;
}
QDockWidget { color: #475467; font-weight: 600; }
QStatusBar { background: #ffffff; border-top: 1px solid #e2e6ee; color: #475467; }
QScrollArea { border: none; background: transparent; }
"""


# --------------------------------------------------------------------- helpers
def _counted(count: int, noun: str, plural: str | None = None) -> str:
    """``1 lead`` / ``3 leads`` — a subtitle should not read like a template."""
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {plural or noun + 's'}"


def _button(text: str, slot: Callable[[], Any], *, tone: str = "") -> QPushButton:
    """A bulk-action button; ``tone`` is the stylesheet object name (Danger…)."""
    button = QPushButton(text)
    if tone:
        button.setObjectName(tone)
    button.clicked.connect(lambda _checked=False: slot())
    return button


def _flag_value(argv: list[str], flag: str) -> str | None:
    """The value following ``flag`` in argv, or None when the flag is absent."""
    if flag not in argv:
        return None
    index = argv.index(flag) + 1
    return argv[index] if index < len(argv) else None


def _clear_layout(layout: QGridLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


def confirm_dialog(parent: QWidget, title: str, text: str) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.setDefaultButton(QMessageBox.No)
    box.button(QMessageBox.Yes).setText("Run it")
    return box.exec_() == QMessageBox.Yes


def _page_header(title: str, subtitle: QLabel, refresh: Callable[[], Any]) -> QHBoxLayout:
    """Title, live subtitle and the page's own Refresh button."""
    head = QHBoxLayout()
    label = QLabel(title)
    label.setObjectName("PageTitle")
    button = QPushButton("Refresh")
    button.clicked.connect(refresh)
    head.addWidget(label)
    head.addWidget(subtitle, 1)
    head.addWidget(button)
    return head


@dataclass
class Step:
    """One CLI run, plus how to decide what follows it.

    ``followup`` receives the finished result and returns the steps to run next,
    which is how a page chains "check eligibility -> enrich -> draft" without
    duplicating the rule in the window.
    """

    argv: list[str]
    json_flag: bool = False
    followup: Callable[[Result], list[Step]] | None = None


@dataclass
class Ctx:
    """What a page may do without knowing the window.

    ``status`` returns the last ``status --json`` payload the window kept, and
    ``refresh_status`` asks for a new one; pages never spawn their own status
    poll, so the strip and the page always read the same snapshot.
    """

    run: Callable[..., bool]
    chain: Callable[..., bool]
    navigate: Callable[..., None]
    notify: Callable[[str, str], None]
    status: Callable[[], dict[str, Any]]
    refresh_status: Callable[[], None]


class Page(QWidget):
    """Base page: refresh-on-show, busy handling and confirmed runs."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._loaded_at = 0.0
        self._splitter: QSplitter | None = None
        self._form: FormPane | None = None
        self._table_min = 160
        self._sized = False

    # ------------------------------------------------------------- lifecycle
    def refresh(self) -> None:
        """Override to load data. Marking freshness happens when a run starts."""

    def attach_form(self, splitter: QSplitter, form: FormPane, table_min: int = 160) -> None:
        """Budget the splitter for the form's real height once the theme is on.

        A page is built before ``app.setStyleSheet`` runs, so measuring the card
        in ``__init__`` under-reports its height and clips the Run button.
        """
        self._splitter = splitter
        self._form = form
        self._table_min = table_min

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.size_splitter()

    def size_splitter(self) -> None:
        if self._sized or self._splitter is None or self._form is None:
            return
        available = self._splitter.height()
        if available < self._table_min + 160:
            # Not laid out yet (first show); try again on the next resize.
            return
        self._sized = True
        form_height = min(self._form.preferred_height(), available - self._table_min)
        self._splitter.setSizes([form_height, available - form_height])

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.size_splitter()

    def note_loaded(self) -> None:
        """Record that data for this page is current (a run actually started)."""
        self._loaded_at = time.monotonic()

    def on_result(self, result: Result) -> None:
        self.note_loaded()

    def on_status(self, payload: dict[str, Any]) -> None:
        """Called for every ``status --json`` payload the window receives."""

    def is_stale(self, seconds: float = 15.0) -> bool:
        return (time.monotonic() - self._loaded_at) > seconds

    def mark_stale(self) -> None:
        self._loaded_at = 0.0

    def on_busy(self, busy: bool) -> None:
        for button in self.findChildren(QPushButton):
            button.setEnabled(not busy)

    # ------------------------------------------------------------------ runs
    def confirm_run(self, spec: Command, values: dict[str, Any]) -> bool:
        try:
            argv = build_argv(spec, values)
        except MissingFields as exc:
            self.ctx.notify("Missing: " + ", ".join(exc.labels), "warn")
            return False
        prompt = confirmation(spec, values)
        if prompt and not confirm_dialog(self, spec.label, prompt):
            return False
        return self.ctx.run(argv, owner=self)

    # ----------------------------------------------------------- lead actions
    def lead_action(self, row: dict[str, Any] | None) -> None:
        """Do the one thing this row's shared state points at.

        ``review`` and ``send`` open the page that owns them; everything else
        runs that stage's own command with this lead id, so a row never needs
        its id copied into another page's form.
        """
        if not row:
            self.ctx.notify("Select a lead first.", "warn")
            return
        page = action_page(row)
        if page:
            self.ctx.navigate(page)
            return
        argv = action_argv(row)
        if not argv:
            self.ctx.notify(str(row.get("reason") or "Nothing to do for this lead."), "muted")
            return
        self.ctx.run(argv, owner=self)


class FormPane(QWidget):
    """A spec-driven command card in a scroll area, for the top of a stage page."""

    run_requested = pyqtSignal(object, object)  # spec, argv

    def __init__(self, spec: Command, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.card = CommandCard(spec, scroll_body=True)
        self.card.run_requested.connect(lambda spec_, argv: self.run_requested.emit(spec_, argv))
        layout.addWidget(self.card)
        # Small enough that a short window can still show the table below; the
        # page hands the splitter the card's natural height as the first size.
        self.setMinimumHeight(160)

    def preferred_height(self) -> int:
        """The card's natural height, so the splitter can budget for it."""
        return min(self.card.sizeHint().height() + 12, 520)

    def set_running(self, running: bool) -> None:
        self.card.set_running(running)

    def apply_values(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.card.form.set_value(key, value)


# ------------------------------------------------------------------- pipeline
class PipelinePage(Page):
    """The home page: what is waiting, what is blocked, and what to do next."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self._doctor_kpi: Kpi | None = None
        self._want_doctor = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(12)

        self.subtitle = muted("reading status…")
        layout.addLayout(_page_header("Pipeline", self.subtitle, self.refresh))

        self.next_card = NextStepCard()
        self.next_card.activated.connect(lambda page: self.ctx.navigate(page))
        layout.addWidget(self.next_card)

        self.blockers = BlockerBanner()
        layout.addWidget(self.blockers)

        self.cards_host = QWidget()
        self.cards = QGridLayout(self.cards_host)
        self.cards.setContentsMargins(0, 0, 0, 0)
        self.cards.setHorizontalSpacing(10)
        self.cards.setVerticalSpacing(10)
        for column in range(4):
            self.cards.setColumnStretch(column, 1)
        layout.addWidget(self.cards_host)

        self.funnel = muted("")
        layout.addWidget(self.funnel)

        self.doctor_table = TablePane("doctor")
        self.runs_table = TablePane("runs")
        tabs = QTabWidget()
        tabs.addTab(self.doctor_table, "Doctor")
        tabs.addTab(self.runs_table, "Run ledger")
        layout.addWidget(tabs, 1)

    def refresh(self) -> None:
        super().refresh()
        self._want_doctor = True
        self.ctx.refresh_status()

    def on_status(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        self.next_card.set_step(next_step(payload))
        self.blockers.set_blockers(blockers_for(payload))
        self.funnel.setText(funnel_line(payload))
        self.runs_table.set_rows(run_rows(payload))
        database = str(payload.get("database") or "")
        self.subtitle.setText(f"{database} · refreshed {time.strftime('%H:%M:%S')}")
        self._fill_cards(payload)
        if self._want_doctor:
            self._want_doctor = False
            self.ctx.run(["doctor", "--json"], json_flag=True, owner=self)

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if result.argv[0] == "doctor" and result.payload:
            self._doctor_kpi = doctor_headline(result.payload)
            self.doctor_table.set_rows(doctor_rows(result.payload))
            self._fill_cards(self.ctx.status())

    def _fill_cards(self, payload: dict[str, Any]) -> None:
        _clear_layout(self.cards)
        cards = status_kpis(payload)
        if self._doctor_kpi is not None:
            cards.insert(0, self._doctor_kpi)
        for index, kpi in enumerate(cards):
            self.cards.addWidget(
                KpiCard(kpi.label, kpi.value, kpi.tone, kpi.hint), index // 4, index % 4
            )


# -------------------------------------------------------------------- collect
class CollectPage(Page):
    """Where leads come from: the two sources, plus what they already stored."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(10)

        self.subtitle = muted("newest leads in the database")
        layout.addLayout(_page_header("1 · Collect", self.subtitle, self.refresh))

        sources = QHBoxLayout()
        sources.setSpacing(10)
        for spec_id in ("source_shopify", "source_olx"):
            card = CommandCard(command(spec_id))
            card.run_requested.connect(lambda _spec, argv: ctx.run(argv, owner=self))
            sources.addWidget(card, 1)
        layout.addLayout(sources)

        bar = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem("any status", None)
        for status in LEAD_STATUSES:
            self.status_filter.addItem(status, status)
        self.source_filter = QComboBox()
        self.source_filter.addItem("any source", None)
        for source in SOURCES:
            self.source_filter.addItem(source, source)
        self.limit = QSpinBox()
        self.limit.setRange(1, 5000)
        self.limit.setValue(200)
        for label, widget in (
            ("Status", self.status_filter),
            ("Source", self.source_filter),
            ("Rows", self.limit),
        ):
            bar.addWidget(QLabel(label))
            bar.addWidget(widget)
        bar.addStretch(1)
        # One click per lead: the row's own next step, never a copied id.
        self.act_button = QPushButton("Run next step")
        self.act_button.setObjectName("Primary")
        self.act_button.clicked.connect(lambda: self.lead_action(self.table.current_row()))
        bar.addWidget(self.act_button)
        layout.addLayout(bar)

        self.table = TablePane("leads")
        self.table.set_placeholder("No leads yet — run a source stage above, then enrich them.")
        self.table.row_activated.connect(self.lead_action)
        layout.addWidget(self.table, 1)

    def refresh(self) -> None:
        super().refresh()
        argv = ["leads", "--json", "--limit", str(self.limit.value())]
        status = self.status_filter.currentData()
        source = self.source_filter.currentData()
        if status:
            argv += ["--status", str(status)]
        if source:
            argv += ["--source", str(source)]
        self.ctx.run(argv, json_flag=True, owner=self)

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if result.argv[0] == "leads" and result.payload:
            rows = rows_of(result.payload)
            self.table.set_rows(rows)
            total = result.payload.get("total")
            self.subtitle.setText(f"{total} leads stored · showing the newest {len(rows)}")

    def on_busy(self, busy: bool) -> None:
        for button in self.findChildren(QPushButton):
            button.setEnabled(not busy)
        for card in self.findChildren(CommandCard):
            card.set_running(busy)


# --------------------------------------------------------------------- enrich
class EnrichPage(Page):
    """The enrichment queue and the addresses it has produced so far."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(8)

        self.subtitle = muted("leads waiting for a storefront")
        layout.addLayout(_page_header("2 · Enrich", self.subtitle, self.refresh))
        self._rows: list[dict[str, Any]] = []

        self.blockers = BlockerBanner()
        layout.addWidget(self.blockers)

        bulk = QHBoxLayout()
        bulk.addWidget(QLabel("Bulk"))
        for count in (25, 50, 200):
            bulk.addWidget(_button(f"Enrich {count}", partial(self._enrich, count)))
        bulk.addWidget(_button("Enrich all", partial(self._enrich, None)))
        bulk.addSpacing(16)
        # Parked leads need their own run: --force is per lead, so retrying them
        # must not re-crawl the whole enriched backlog on the way.
        self.retry_button = _button("Retry parked", self._retry_parked)
        self.retry_button.setEnabled(False)
        bulk.addWidget(self.retry_button)
        bulk.addStretch(1)
        layout.addLayout(bulk)

        splitter = QSplitter(Qt.Vertical)
        self.form = FormPane(command("enrich"))
        self.form.run_requested.connect(self._run)
        splitter.addWidget(self.form)

        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(170)
        self.table = TablePane("leads")
        self.table.set_placeholder("Nothing to enrich — every lead is drafted, parked or finished.")
        self.table.row_activated.connect(self.lead_action)
        self.contacts = TablePane("contacts")
        self.tabs.addTab(self.table, "Queue")
        self.tabs.addTab(self.contacts, "Contacts")
        self.tabs.currentChanged.connect(lambda _index: self.refresh())
        splitter.addWidget(self.tabs)
        self.attach_form(splitter, self.form, table_min=200)
        layout.addWidget(splitter, 1)

        layout.addWidget(
            muted(
                "Deep resolve is ticked by default: without it, a lead whose shop "
                "subdomain cannot be guessed is parked after a single attempt. "
                "Parked leads stay in the queue — tick Force to retry them."
            )
        )

    def refresh(self) -> None:
        super().refresh()
        if not self.ctx.status():
            self.ctx.refresh_status()
        if self.tabs.currentIndex() == 0:
            self.ctx.run(["leads", "--json", "--limit", "500"], json_flag=True, owner=self)
        else:
            self.ctx.run(["contacts", "--json", "--limit", "300"], json_flag=True, owner=self)

    def on_status(self, payload: dict[str, Any]) -> None:
        self.blockers.set_blockers(blockers_for(payload, "enrich"))

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if result.argv[0] == "leads" and result.payload:
            rows = rows_where_action(result.payload, ("enrich", "retry"))
            self._rows = rows
            self.table.set_rows(rows)
            awaiting = sum(1 for row in rows if row.get("next_action") == "enrich")
            parked = len(rows) - awaiting
            self.subtitle.setText(
                f"{awaiting} awaiting enrichment"
                + (f" · {parked} parked (tick Force to retry)" if parked else "")
            )
            self.retry_button.setText(f"Retry parked ({parked})")
            self.retry_button.setEnabled(parked > 0)
        elif result.argv[0] == "contacts" and result.payload:
            self.contacts.set_rows(rows_of(result.payload))
        elif result.argv[0] == "enrich":
            self.ctx.refresh_status()
            self.refresh()

    def _enrich(self, count: int | None) -> None:
        """Enrich the next ``count`` leads, deep-resolving like the form does."""
        argv = ["enrich", "--deep-resolve"]
        if count:
            argv += ["--limit", str(count)]
        if self.ctx.run(argv, owner=self):
            self.form.set_running(True)

    def _retry_parked(self) -> None:
        """Re-run the parked leads only: one forced pass each, not the backlog.

        ``enrich --force`` selects every new/enriched/skipped lead, so a bulk
        retry through the flag alone would re-crawl hundreds of finished shops
        to reach a handful of parked ones. Scoping by lead id is the same
        command the row's own Retry action runs.
        """
        parked = [row for row in self._rows if row.get("next_action") == "retry"]
        if not parked:
            self.ctx.notify("No parked leads to retry.", "muted")
            return
        steps = [
            Step(["enrich", "--lead-id", str(row.get("id")), "--deep-resolve", "--force"])
            for row in parked
        ]
        if self.ctx.chain(steps, owner=self):
            self.form.set_running(True)
            self.ctx.notify(f"Retrying {_counted(len(parked), 'parked lead')}.", "info")

    def _run(self, _spec: Command, argv: list[str]) -> None:
        if self.ctx.run(argv, owner=self):
            self.form.set_running(True)

    def on_busy(self, busy: bool) -> None:
        super().on_busy(busy)
        if not busy:
            self.form.set_running(False)
            # on_busy re-enables every button; a bulk action with nothing to do
            # must go back to being unavailable.
            parked = sum(1 for row in self._rows if row.get("next_action") == "retry")
            self.retry_button.setEnabled(parked > 0)

    def apply_values(self, values: dict[str, Any]) -> None:
        self.form.apply_values(values)


# ---------------------------------------------------------------------- draft
class DraftPage(Page):
    """Compose, with the eligibility preview the CLI's ``--check`` provides."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(8)

        self.subtitle = muted("checking what is ready to draft…")
        layout.addLayout(_page_header("3 · Draft", self.subtitle, self.refresh))
        self._rows: list[dict[str, Any]] = []

        self.blockers = BlockerBanner()
        layout.addWidget(self.blockers)

        bulk = QHBoxLayout()
        self.batch = QSpinBox()
        self.batch.setRange(1, 500)
        self.batch.setValue(3)
        bulk.addWidget(QLabel("Draft"))
        bulk.addWidget(self.batch)
        bulk.addWidget(_button("Draft N", self._draft_batch))
        # One click for the whole eligible batch: the count is the check's, so
        # the label and the run cannot disagree about how much work this is.
        self.draft_all = _button("Draft all ready", self._draft_all)
        self.draft_all.setEnabled(False)
        bulk.addWidget(self.draft_all)
        bulk.addStretch(1)
        layout.addLayout(bulk)

        splitter = QSplitter(Qt.Vertical)
        self.form = FormPane(command("compose"))
        self.form.run_requested.connect(self._run)
        splitter.addWidget(self.form)
        self.preview = TablePane("compose_check")
        self.preview.setMinimumHeight(170)
        self.preview.set_placeholder(
            "No lead is ready to draft — enrich first, or clear the blockers above."
        )
        self.preview.row_activated.connect(self._activate)
        splitter.addWidget(self.preview)
        self.attach_form(splitter, self.form, table_min=170)
        layout.addWidget(splitter, 1)

        layout.addWidget(
            muted(
                "Compose needs the sender fields in .env and a running Ollama. "
                "The table is `compose --check`: eligibility only, no model call."
            )
        )

    def refresh(self) -> None:
        super().refresh()
        self.ctx.run(["compose", "--check", "--json"], json_flag=True, owner=self)

    def on_status(self, payload: dict[str, Any]) -> None:
        self.blockers.set_blockers(blockers_for(payload, "draft"))

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if result.argv[:2] == ["compose", "--check"] and result.payload:
            rows = rows_of(result.payload, "leads")
            self._rows = rows
            self.preview.set_rows(rows)
            # Count what the rows say, not a separate scalar: a bulk button that
            # claims work the preview cannot show is the bug this page exists for.
            ready = sum(1 for row in rows if row.get("next_action") == "draft")
            self.subtitle.setText(
                f"{_counted(ready, 'lead')} ready to draft · {len(rows)} checked"
            )
            self.draft_all.setText(f"Draft all ready ({ready})")
            self.draft_all.setEnabled(ready > 0)
        elif result.argv[0] == "compose":
            self.ctx.refresh_status()
            self.refresh()

    def _draft_batch(self) -> None:
        if self.ctx.run(["compose", "--limit", str(self.batch.value())], owner=self):
            self.form.set_running(True)

    def _draft_all(self) -> None:
        if self.ctx.run(["compose"], owner=self):
            self.form.set_running(True)

    def draft_lead(self, lead_id: int, argv: list[str] | None = None) -> None:
        """Draft one lead, enriching it first when the shared rule says so.

        The chain asks ``compose --check`` what the lead needs, and enriches at
        most once. Everything else (already drafted, suppressed, failed cap)
        falls through to ``compose``, whose no-op note names the rule that
        rejected it -- the silent ``scanned 0`` was the original complaint.

        ``argv`` is the operator's own command when it came from the form, so
        flags like ``--regenerate`` survive the detour through the check.
        """
        compose_argv = list(argv) if argv else ["compose", "--lead-id", str(lead_id)]
        check_argv = [compose_argv[0], "--check", "--json", *compose_argv[1:]]
        tried = {"enriched": False}

        def after_check(result: Result) -> list[Step]:
            row = next(
                (
                    item
                    for item in rows_of(result.payload, "leads")
                    if str(item.get("lead_id")) == str(lead_id)
                ),
                {},
            )
            if row.get("next_action") == "enrich" and not tried["enriched"]:
                tried["enriched"] = True
                return [
                    Step(["enrich", "--lead-id", str(lead_id), "--deep-resolve"]),
                    Step(check_argv, json_flag=True, followup=after_check),
                ]
            return [Step(compose_argv)]

        steps = [Step(check_argv, json_flag=True, followup=after_check)]
        if self.ctx.chain(steps, owner=self):
            self.form.set_running(True)

    def _activate(self, row: object) -> None:
        """Double-click a checked lead: draft it, or say why it cannot be."""
        if not isinstance(row, dict):
            return
        if row.get("next_action") == "draft":
            self.draft_lead(int(row["lead_id"]))
        else:
            self.ctx.notify(str(row.get("reason") or "Nothing to draft here."), "muted")

    def _run(self, _spec: Command, argv: list[str]) -> None:
        # A single-lead run goes through the chain: compose alone would no-op
        # on a freshly collected lead and leave the operator guessing.
        lead_id = _flag_value(argv, "--lead-id")
        if lead_id is not None:
            self.draft_lead(int(lead_id), argv)
            return
        if self.ctx.run(argv, owner=self):
            self.form.set_running(True)

    def on_busy(self, busy: bool) -> None:
        super().on_busy(busy)
        if not busy:
            self.form.set_running(False)
            eligible = sum(1 for row in self._rows if row.get("next_action") == "draft")
            self.draft_all.setEnabled(eligible > 0)

    def apply_values(self, values: dict[str, Any]) -> None:
        self.form.apply_values(values)


# -------------------------------------------------------------------- approve
class ApprovePage(Page):
    """Read a draft, then approve it: the only human gate in the pipeline."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(8)

        self.subtitle = muted("composed drafts wait here")
        layout.addLayout(_page_header("4 · Approve", self.subtitle, self.refresh))
        self._rows: list[dict[str, Any]] = []

        bar = QHBoxLayout()
        self.limit = QSpinBox()
        self.limit.setRange(1, 2000)
        self.limit.setValue(200)
        bar.addWidget(QLabel("Rows"))
        bar.addWidget(self.limit)
        bar.addWidget(QLabel("Show"))
        self.filter = QComboBox()
        # `drafts` lists every message; this page is about the human gate, so it
        # opens on the composed ones and keeps queued ones one click away.
        for label, value in (
            ("awaiting review", "composed"),
            ("queued", "queued"),
            ("all drafts", None),
        ):
            self.filter.addItem(label, value)
        self.filter.currentIndexChanged.connect(lambda _index: self._fill())
        bar.addWidget(self.filter)
        bar.addStretch(1)
        # Bulk approval sits next to the single one: same gate, same review list.
        self.approve_all = _button("Approve all (0)", self._approve_all)
        self.approve_all.setEnabled(False)
        bar.addWidget(self.approve_all)
        for text, slot in (
            ("Approve", self._approve),
            ("Unapprove", self._unapprove),
            ("Suppress email", self._suppress),
            ("Copy body", self._copy_body),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        self.table = TablePane("drafts")
        self.detail = QPlainTextEdit()
        self.detail.setObjectName("Draft")
        self.detail.setReadOnly(True)
        splitter.addWidget(self.table)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        # Reviewing wording needs width: the body gets a real column, not a strip.
        splitter.setSizes([700, 640])
        layout.addWidget(splitter, 1)

        self.table.row_selected.connect(self._show_detail)

    def refresh(self) -> None:
        super().refresh()
        self.ctx.run(
            ["drafts", "--json", "--limit", str(self.limit.value())],
            json_flag=True,
            owner=self,
        )

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if result.argv[0] == "drafts" and result.payload:
            self._rows = rows_of(result.payload)
            self._fill()
        elif result.argv[0] in {"approve", "unapprove", "suppress"}:
            self.ctx.refresh_status()
            self.refresh()

    def _fill(self) -> None:
        wanted = self.filter.currentData()
        rows = [row for row in self._rows if wanted is None or row.get("lead_status") == wanted]
        self.table.set_rows(rows)
        composed = sum(1 for row in self._rows if row.get("lead_status") == "composed")
        queued = sum(1 for row in self._rows if row.get("lead_status") == "queued")
        self.subtitle.setText(f"{composed} awaiting review · {queued} queued · showing {len(rows)}")
        self.approve_all.setText(f"Approve all ({composed})")
        self.approve_all.setEnabled(composed > 0)

    def on_busy(self, busy: bool) -> None:
        super().on_busy(busy)
        if not busy:
            composed = sum(1 for row in self._rows if row.get("lead_status") == "composed")
            self.approve_all.setEnabled(composed > 0)

    def _show_detail(self, row: object) -> None:
        if not isinstance(row, dict):
            self.detail.clear()
            return
        header = (
            f"lead {row.get('lead_id')} · message {row.get('message_id')} · "
            f"{row.get('contact')} · {row.get('lead_status')} · "
            f"{cell_text(row.get('created_at'))}\n"
            f"subject: {row.get('subject')}\n"
            f"model: {row.get('model')} · prompt: {row.get('prompt_version')} · "
            f"footer: {row.get('footer_version')}\n\n"
        )
        self.detail.setPlainText(header + str(row.get("body") or ""))

    def _selected(self) -> dict[str, Any] | None:
        row = self.table.current_row()
        if row is None:
            self.ctx.notify("Select a draft first.", "warn")
        return row

    def _approve(self) -> None:
        row = self._selected()
        if row:
            self.confirm_run(
                command("approve"),
                {"lead_id": row["lead_id"], "message_id": row["message_id"]},
            )

    def _approve_all(self) -> None:
        """Approve every composed draft the table is showing, after a review list."""
        rows = [row for row in self._rows if row.get("lead_status") == "composed"]
        if not rows:
            self.ctx.notify("Nothing is awaiting review.", "muted")
            return
        lines = []
        for row in rows[:12]:
            subject = " ".join(str(row.get("subject") or "").split())[:58]
            lines.append(
                f"lead {row.get('lead_id')} · {row.get('company') or '-'} · "
                f"{row.get('contact')} · {subject}"
            )
        if len(rows) > 12:
            lines.append(f"… and {len(rows) - 12} more")
        truncated = (
            f"\n\nThe table is capped at {self.limit.value()} rows; only these are approved."
            if len(self._rows) >= self.limit.value()
            else ""
        )
        if not confirm_dialog(
            self,
            "Approve all",
            f"Approve {_counted(len(rows), 'draft')}?\n\n"
            + "\n".join(lines)
            + truncated
            + "\n\nEach lead moves to `queued` and may be delivered by the next send run.",
        ):
            return
        self.ctx.run(["approve", "--all-composed", "--yes", "--limit", str(len(rows))], owner=self)

    def _unapprove(self) -> None:
        row = self._selected()
        if row:
            self.confirm_run(command("unapprove"), {"lead_id": row["lead_id"]})

    def _suppress(self) -> None:
        row = self._selected()
        if not row:
            return
        email = row.get("contact")
        if not email:
            self.ctx.notify("This draft has no contact address to suppress.", "warn")
            return
        self.confirm_run(
            command("suppress"),
            {"email": email, "reason": "operator opt-out from GUI"},
        )

    def _copy_body(self) -> None:
        row = self.table.current_row()
        if not row:
            return
        QApplication.clipboard().setText(str(row.get("body") or ""))
        self.ctx.notify("Draft body copied to clipboard.", "ok")


# ----------------------------------------------------------------------- send
class SendPage(Page):
    """The approved queue, the sending policy and the send form."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(8)

        self.subtitle = muted("reading policy…")
        layout.addLayout(_page_header("5 · Send", self.subtitle, self.refresh))
        self._drafts: list[dict[str, Any]] = []

        self.blockers = BlockerBanner()
        layout.addWidget(self.blockers)

        bulk = QHBoxLayout()
        self.batch = QSpinBox()
        self.batch.setRange(1, 100)
        self.batch.setValue(5)
        bulk.addWidget(QLabel("Batch"))
        bulk.addWidget(self.batch)
        bulk.addWidget(_button("Dry run", self._dry_run))
        live = _button("Send live", self._send_live, tone="Danger")
        live.setToolTip(
            "Real SMTP delivery. Caps, the sending window and suppressions are "
            "still enforced by the CLI."
        )
        bulk.addWidget(live)
        bulk.addStretch(1)
        layout.addLayout(bulk)

        splitter = QSplitter(Qt.Vertical)
        self.form = FormPane(command("send"))
        self.form.run_requested.connect(self._run)
        splitter.addWidget(self.form)
        self.queue = TablePane("leads")
        self.queue.setMinimumHeight(130)
        self.queue.set_placeholder("No approved mail waiting — approve a draft on stage 4 first.")
        splitter.addWidget(self.queue)
        self.attach_form(splitter, self.form, table_min=110)
        layout.addWidget(splitter, 1)

    def refresh(self) -> None:
        super().refresh()
        self.ctx.refresh_status()
        self.ctx.run(
            ["leads", "--json", "--status", "queued", "--limit", "200"], json_flag=True, owner=self
        )
        # The recipient list behind the live-send confirmation: `leads` names the
        # queue, but only `drafts` carries the address each message goes to.
        self.ctx.run(["drafts", "--json", "--limit", "200"], json_flag=True, owner=self)

    def on_status(self, payload: dict[str, Any]) -> None:
        self.blockers.set_blockers(blockers_for(payload, "send"))
        window = payload.get("window") or {}
        cap = window.get("daily_cap") if isinstance(window, dict) else None
        kill_switch = payload.get("kill_switch")
        self.subtitle.setText(
            f"kill switch: {kill_switch} · window: {window_text(window)}"
            + (f" · daily cap {cap}" if cap is not None else "")
        )

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if result.argv[0] == "leads" and result.payload:
            rows = rows_of(result.payload)
            self.queue.set_rows(rows)
        elif result.argv[0] == "drafts" and result.payload:
            self._drafts = rows_of(result.payload)
        elif result.argv[0] == "send":
            self.ctx.refresh_status()
            self.refresh()

    def _dry_run(self) -> None:
        """Render .eml evidence for a batch; nothing leaves the machine."""
        count = self.batch.value()
        if self.ctx.run(["send", "--yes", "--limit", str(count)], owner=self):
            self.form.set_running(True)

    def _send_live(self) -> None:
        """Deliver for real, behind a recipient list: this cannot be undone."""
        count = self.batch.value()
        queued = [row for row in self._drafts if row.get("lead_status") == "queued"]
        lines = [
            f"lead {row.get('lead_id')} · {row.get('company') or '-'} · {row.get('contact')}"
            for row in queued[:8]
        ]
        if len(queued) > 8:
            lines.append(f"… and {len(queued) - 8} more")
        listed = "\n".join(lines) if lines else "No approved draft is loaded — press Refresh."
        if not confirm_dialog(
            self,
            "Send live",
            f"Deliver up to {_counted(count, 'real email')} over SMTP?\n\n"
            f"{listed}\n\n"
            "This cannot be undone. The kill switch, the sending window, the daily cap "
            "and suppressions are still enforced by the CLI.",
        ):
            return
        if self.ctx.run(["send", "--live", "--yes", "--limit", str(count)], owner=self):
            self.form.set_running(True)

    def _run(self, _spec: Command, argv: list[str]) -> None:
        if self.ctx.run(argv, owner=self):
            self.form.set_running(True)

    def on_busy(self, busy: bool) -> None:
        super().on_busy(busy)
        if not busy:
            self.form.set_running(False)

    def apply_values(self, values: dict[str, Any]) -> None:
        self.form.apply_values(values)


# -------------------------------------------------------------------- replies
class RepliesPage(Page):
    """Read-only IMAP sync plus the inbound review ledger."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(10)

        self.subtitle = muted("read-only IMAP: EXAMINE + BODY.PEEK")
        layout.addLayout(_page_header("6 · Replies", self.subtitle, self.refresh))

        self.card = CommandCard(command("inbox_sync"))
        self.card.run_requested.connect(lambda _spec, argv: ctx.run(argv, owner=self))
        layout.addWidget(self.card)

        bar = QHBoxLayout()
        for text, slot in (
            ("Show", self._show),
            ("Apply", self._apply),
            ("Apply all", self._apply_all),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.table = TablePane("inbound")
        layout.addWidget(self.table, 1)

    def refresh(self) -> None:
        super().refresh()
        self.ctx.run(["inbox", "list", "--json", "--limit", "200"], json_flag=True, owner=self)

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if result.argv[:2] == ["inbox", "list"] and result.payload:
            rows = rows_of(result.payload)
            self.table.set_rows(rows)
            needs_review = sum(1 for row in rows if row.get("status") == "needs_review")
            self.subtitle.setText(
                f"{_counted(len(rows), 'inbound row')} · {needs_review} need review"
            )
        elif result.argv[:2] in (["inbox", "sync"], ["inbox", "apply"]):
            self.ctx.refresh_status()
            self.refresh()

    def on_busy(self, busy: bool) -> None:
        for button in self.findChildren(QPushButton):
            button.setEnabled(not busy)
        for card in self.findChildren(CommandCard):
            card.set_running(busy)

    def _show(self) -> None:
        row = self.table.current_row()
        if not row:
            self.ctx.notify("Select an inbound message first.", "warn")
            return
        self.ctx.run(["inbox", "show", "--id", str(row["id"])], owner=self)

    def _apply(self) -> None:
        row = self.table.current_row()
        if not row:
            self.ctx.notify("Select an inbound message first.", "warn")
            return
        self.confirm_run(command("inbox_apply"), {"id": row["id"]})

    def _apply_all(self) -> None:
        self.confirm_run(command("inbox_apply"), {"all": True})


# ---------------------------------------------------------------------- tools
class ToolsPage(Page):
    """Ledger, maintenance and manual overrides, off the pipeline path."""

    MAINTENANCE = (
        "db_init",
        "doctor",
        "backup",
        "mark",
        "suppress",
        "unsuppress",
        "stats",
        "version",
    )

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(10)

        self.subtitle = muted("ledger, maintenance and manual overrides")
        layout.addLayout(_page_header("Tools", self.subtitle, self.refresh))

        self.tabs = QTabWidget()
        self.tabs.addTab(self._ledger_tab(), "Ledger")
        self.tabs.addTab(self._maintenance_tab(), "Maintenance")
        self.tabs.currentChanged.connect(lambda _index: self.refresh())
        layout.addWidget(self.tabs, 1)

    def _ledger_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.addStretch(1)
        resolve = QPushButton("Resolve send…")
        resolve.clicked.connect(self._resolve)
        open_file = QPushButton("Open .eml")
        open_file.clicked.connect(self._open_eml)
        bar.addWidget(resolve)
        bar.addWidget(open_file)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Vertical)
        self.sends_table = TablePane("sends")
        self.sends_table.set_placeholder(
            "No send attempt yet — a dry run writes .eml files to the outbox below."
        )
        self.outbox_table = TablePane("outbox")
        splitter.addWidget(self.sends_table)
        splitter.addWidget(self.outbox_table)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 360])
        layout.addWidget(splitter, 1)
        return tab

    def _maintenance_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)

        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        for index, spec_id in enumerate(self.MAINTENANCE):
            card = CommandCard(command(spec_id))
            card.run_requested.connect(lambda _spec, argv: self.ctx.run(argv, owner=self))
            grid.addWidget(card, index // 2, index % 2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)
        return tab

    def refresh(self) -> None:
        super().refresh()
        if self.tabs.currentIndex() == 0:
            self.ctx.run(["sends", "--json", "--limit", "200"], json_flag=True, owner=self)

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if result.argv[0] == "sends" and result.payload:
            self.sends_table.set_rows(rows_of(result.payload))
            self.ctx.run(["outbox", "--json", "--limit", "200"], json_flag=True, owner=self)
        elif result.argv[0] == "outbox" and result.payload:
            self.outbox_table.set_rows(rows_of(result.payload))
        elif result.argv[0] in {"mark", "suppress", "unsuppress"}:
            self.ctx.refresh_status()

    def on_busy(self, busy: bool) -> None:
        for button in self.findChildren(QPushButton):
            button.setEnabled(not busy)
        for card in self.findChildren(CommandCard):
            card.set_running(busy)

    def _resolve(self) -> None:
        row = self.sends_table.current_row()
        if not row:
            self.ctx.notify("Select a send first.", "warn")
            return
        dialog = MarkDialog("Resolve send attempt", ("sent", "failed"), self)
        if dialog.exec_():
            status, reason = dialog.values()
            values: dict[str, Any] = {"send_id": row["id"], "status": status}
            if reason:
                values["reason"] = reason
            self.confirm_run(command("mark"), values)

    def _open_eml(self) -> None:
        row = self.outbox_table.current_row()
        if not row or not row.get("path"):
            self.ctx.notify("Select an .eml file first.", "warn")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(row["path"])))


# ------------------------------------------------------------------ main window
NAV_PAGES: tuple[tuple[str, str], ...] = (
    ("pipeline", "Pipeline"),
    ("collect", "1 · Collect"),
    ("enrich", "2 · Enrich"),
    ("draft", "3 · Draft"),
    ("approve", "4 · Approve"),
    ("send", "5 · Send"),
    ("replies", "6 · Replies"),
    ("tools", "Tools"),
)

#: Stages whose completion can change the funnel, so the strip is re-read.
MUTATING_STAGES = frozenset(
    {
        "source",
        "enrich",
        "compose",
        "approve",
        "unapprove",
        "send",
        "inbox",
        "mark",
        "suppress",
        "unsuppress",
    }
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Forgesmith {__version__} — B2B pipeline console")
        self.resize(1420, 920)

        self.runner = CliRunner(self)
        self.runner.started.connect(self._on_started)
        self.runner.chunk.connect(self._on_chunk)
        self.runner.finished.connect(self._on_finished)

        self._status: dict[str, Any] = {}
        self._queue: deque[tuple[Step, Page | None]] = deque()
        self._step: Step | None = None
        self._owner: Page | None = None
        self.ctx = Ctx(
            run=self.run_stage,
            chain=self.run_chain,
            navigate=self.navigate,
            notify=self.notify,
            status=lambda: self._status,
            refresh_status=self.refresh_status,
        )

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._header())
        outer.addWidget(self._stage_bar())

        self.stack = QStackedWidget()
        self.pages: dict[str, Any] = {
            "pipeline": PipelinePage(self.ctx),
            "collect": CollectPage(self.ctx),
            "enrich": EnrichPage(self.ctx),
            "draft": DraftPage(self.ctx),
            "approve": ApprovePage(self.ctx),
            "send": SendPage(self.ctx),
            "replies": RepliesPage(self.ctx),
            "tools": ToolsPage(self.ctx),
        }
        for page_id, _label in NAV_PAGES:
            self.stack.addWidget(self.pages[page_id])
        self._page_ids = [page_id for page_id, _label in NAV_PAGES]

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 12, 12, 12)
        body_layout.addWidget(self.stack)
        outer.addWidget(body, 1)
        self.setCentralWidget(central)

        self.console = ConsolePane()
        dock = QDockWidget("Console", self)
        dock.setObjectName("ConsoleDock")
        dock.setWidget(self.console)
        dock.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.TopDockWidgetArea)
        # The pane renders its own header; an empty title bar avoids a second
        # "Console" caption in the platform's default (light) styling.
        dock.setTitleBarWidget(QWidget())
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [180], Qt.Vertical)

        self.stack.setCurrentIndex(0)
        self._sync_nav("pipeline")
        self.statusBar().showMessage("Ready")

        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start()

        QTimer.singleShot(200, self._startup)

    # ------------------------------------------------------------------ chrome
    def _header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)

        name = QLabel("Forgesmith")
        name.setObjectName("AppName")
        tag = QLabel(f"{__version__} · local Ollama · approval-gated SMTP")
        tag.setObjectName("AppTag")

        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        refresh = QPushButton("Refresh")
        refresh.setShortcut("F5")
        refresh.clicked.connect(self._refresh_current)

        layout.addWidget(name)
        layout.addWidget(tag)
        layout.addStretch(1)
        layout.addWidget(self.stop_button)
        layout.addWidget(refresh)
        return header

    def _stage_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("StageBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self._extra_nav: dict[str, QPushButton] = {}
        pipeline = self._tab_button("pipeline", "Pipeline")
        layout.addWidget(pipeline)

        self.strip = StageStrip(STAGES)
        self.strip.page_selected.connect(lambda page: self.navigate(page))
        layout.addWidget(self.strip, 1)

        layout.addWidget(self._tab_button("tools", "Tools"))
        return bar

    def _tab_button(self, page_id: str, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("StageTab")
        button.setCursor(Qt.PointingHandCursor)
        button.setProperty("active", False)
        button.clicked.connect(lambda _checked=False, target=page_id: self.navigate(target))
        self._extra_nav[page_id] = button
        return button

    # ------------------------------------------------------------------- runs
    def run_stage(
        self,
        argv: list[str],
        *,
        json_flag: bool = False,
        owner: Page | None = None,
        followup: Callable[[Result], list[Step]] | None = None,
    ) -> bool:
        """Start one stage, or queue it behind the running one.

        The queue is what makes a chain like enrich -> compose safe: one process
        at a time, in the order the pages asked for them. ``followup`` may add
        steps once this one finishes, which is how "draft this lead, enriching it
        first" stays a single click without the GUI guessing eligibility.
        """
        step = Step(list(argv), json_flag=json_flag, followup=followup)
        return self.run_chain([step], owner=owner)

    def run_chain(self, steps: list[Step], *, owner: Page | None = None) -> bool:
        """Queue a short sequence of stages; the first one starts now if idle."""
        if not steps:
            return False
        busy = self.runner.busy or bool(self._queue)
        self._queue.extend((step, owner) for step in steps)
        if busy:
            if owner is not None and steps[0].argv[:1] != ["status"]:
                self.notify(
                    f"Queued ({len(self._queue)}): forgesmith {' '.join(steps[0].argv)}",
                    "info",
                )
            return True
        step, step_owner = self._queue.popleft()
        return self._start(step, owner=step_owner)

    def _start(self, step: Step, *, owner: Page | None = None) -> bool:
        self._step = step
        self._owner = owner
        self.console.begin("forgesmith " + " ".join(step.argv))
        self._set_busy(True)
        started = self.runner.start(step.argv, json_flag=step.json_flag)
        if not started:
            self._set_busy(False)
            self.notify("Could not start the stage.", "bad")
            return False
        note = getattr(owner, "note_loaded", None)
        if callable(note):
            note()
        return True

    def refresh_status(self) -> None:
        """Ask for a fresh ``status --json`` unless one is already in flight."""
        if self.runner.busy and self.runner.argv[:1] == ["status"]:
            return
        if any(item[0].argv[:1] == ["status"] for item in self._queue):
            return
        self.run_stage(["status", "--json"], json_flag=True, owner=None)

    def _apply_status(self, payload: dict[str, Any]) -> None:
        """Keep one snapshot for the strip and every page that wants it."""
        self._status = payload
        self.strip.set_counts(stage_counts(payload))
        for page in self.pages.values():
            handler = getattr(page, "on_status", None)
            if callable(handler):
                self._safe(handler, payload)

    def _on_started(self, command_line: str) -> None:
        self.statusBar().showMessage(f"Running {command_line}…")

    def _on_chunk(self, text: str, channel: str) -> None:
        self.console.append(text, channel)

    def _on_finished(self, result: Result) -> None:
        self.console.finish(result)
        owner, self._owner = self._owner, None
        self._set_busy(False)

        if result.argv[:1] == ["status"] and result.payload:
            self._safe(self._apply_status, result.payload)

        if owner is not None and hasattr(owner, "on_result"):
            try:
                owner.on_result(result)
            except Exception as exc:  # a page bug must not kill the console
                self.console.append(f"\n[gui] {type(exc).__name__}: {exc}\n", "err")

        if result.argv[:1] != ["status"]:
            label, tone = exit_tone(result.exit_code)
            self.notify(f"{result.command_line} → {label} ({result.duration_s}s)", tone)
            if not result.cancelled and result.argv[:1] and result.argv[0] in MUTATING_STAGES:
                self._safe(self.refresh_status)

        current = self.stack.currentWidget()
        for page in self.pages.values():
            if page is not owner and page is not current and hasattr(page, "mark_stale"):
                page.mark_stale()

        step, self._step = self._step, None
        if step is not None and step.followup is not None and not result.cancelled:
            try:
                chained = step.followup(result)
            except Exception as exc:  # a bad follow-up must not kill the console
                self.console.append(f"\n[gui] chain: {type(exc).__name__}: {exc}\n", "err")
                chained = []
            if chained:
                # The chain continues before anything the pages queued.
                self._queue.extendleft((item, owner) for item in reversed(chained))

        if self._queue:
            queued_step, queued_owner = self._queue.popleft()
            self._start(queued_step, owner=queued_owner)
            return

        self._set_busy(False)
        # A page opened while another stage was running had its refresh
        # refused, so it would sit on its placeholder until F5. Pick it up now.
        if current is not owner and getattr(current, "is_stale", lambda: False)():
            self._safe(current.refresh)

    def _stop(self) -> None:
        if self._queue:
            self._queue.clear()
            self.notify("Cleared the queued stages.", "warn")
        if self.runner.busy:
            self.notify("Cancelling the running stage…", "warn")
            self.runner.cancel()

    def _set_busy(self, busy: bool) -> None:
        self.stop_button.setEnabled(busy)
        for page in self.pages.values():
            if hasattr(page, "on_busy"):
                page.on_busy(busy)

    # ---------------------------------------------------------------- routing
    def navigate(self, page_id: str, values: dict[str, Any] | None = None) -> None:
        if page_id not in self._page_ids:
            return
        self.stack.setCurrentIndex(self._page_ids.index(page_id))
        self._sync_nav(page_id)
        page = self.pages[page_id]
        if values and hasattr(page, "apply_values"):
            page.apply_values(values)
        if getattr(page, "is_stale", lambda: False)():
            self._safe(page.refresh)

    def _sync_nav(self, page_id: str) -> None:
        self.strip.set_active(page_id)
        for key, button in self._extra_nav.items():
            button.setProperty("active", key == page_id)
            repolish(button)

    def _refresh_current(self) -> None:
        page = self.stack.currentWidget()
        if hasattr(page, "refresh"):
            self._safe(page.refresh)
        self.refresh_status()

    def _auto_refresh(self) -> None:
        if self.runner.busy or self._queue:
            return
        if self.stack.currentWidget() is self.pages["pipeline"]:
            self._safe(self.pages["pipeline"].refresh)

    def _startup(self) -> None:
        self._safe(self.pages["pipeline"].refresh)

    def _safe(self, action: Callable[..., Any], *args: Any) -> None:
        """Run a slot body; PyQt5 aborts the process on unhandled slot errors."""
        try:
            action(*args)
        except Exception as exc:
            self.console.append(f"\n[gui] {type(exc).__name__}: {exc}\n", "err")
            self.notify(f"GUI error: {exc}", "bad")

    def notify(self, message: str, tone: str = "muted") -> None:
        self.statusBar().showMessage(message, 10_000)
        self.statusBar().setStyleSheet(f"color: {tone_color(tone)};")

    # ------------------------------------------------------------------ close
    def closeEvent(self, event: QCloseEvent) -> None:
        if self.runner.busy:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("A stage is running")
            box.setText("Stop the running stage and quit?")
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            if box.exec_() != QMessageBox.Yes:
                event.ignore()
                return
            self.runner.cancel()
        self._queue.clear()
        event.accept()


def run(argv: list[str] | None = None) -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication([sys.argv[0], *(argv or [])])
    app.setApplicationName("Forgesmith")
    app.setApplicationDisplayName("Forgesmith")
    app.setStyle("Fusion")
    font = QFont("Segoe UI", 9)
    app.setFont(font)
    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    window.show()
    return app.exec_()
