"""Forgesmith desktop console: every stage, ledger and status in one window."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QCloseEvent, QDesktopServices, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
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
    COMMANDS,
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
    Kpi,
    doctor_headline,
    doctor_rows,
    rows_of,
    run_rows,
    status_kpis,
    status_strip,
    window_text,
)
from forgesmith.gui.runner import CliRunner, Result
from forgesmith.gui.widgets import (
    CommandCard,
    ConsolePane,
    KpiCard,
    MarkDialog,
    TablePane,
    muted,
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
QListWidget#Nav {
    background: #ffffff; border: 1px solid #e2e6ee; border-radius: 10px; padding: 6px;
    outline: none;
}
QListWidget#Nav::item { padding: 9px 10px; border-radius: 6px; color: #344054; }
QListWidget#Nav::item:hover { background: #f1f4f9; }
QListWidget#Nav::item:selected { background: #e8efff; color: #1d4ed8; font-weight: 600; }
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
QPushButton#Danger:disabled { background: #f3d9d9; color: #c98a8a; }
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


@dataclass
class Ctx:
    run: Callable[..., bool]
    navigate: Callable[..., None]
    notify: Callable[[str, str], None]


class Page(QWidget):
    """Base page: refresh-on-show, busy handling and confirmed runs."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._loaded_at = 0.0

    # ------------------------------------------------------------- lifecycle
    def refresh(self) -> None:
        """Override to load data. Marking freshness happens when a run starts."""

    def note_loaded(self) -> None:
        """Record that data for this page is current (a run actually started)."""
        self._loaded_at = time.monotonic()

    def on_result(self, result: Result) -> None:
        self.note_loaded()

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


class CommandPage(Page):
    """A single spec-driven form."""

    def __init__(
        self,
        ctx: Ctx,
        spec_id: str,
        parent: QWidget | None = None,
        extra: QWidget | None = None,
    ) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)

        self.card = CommandCard(command(spec_id))
        self.card.run_requested.connect(self._run)

        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(12)
        if extra is not None:
            host_layout.addWidget(extra)
        host_layout.addWidget(self.card)
        host_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

    def _run(self, spec: Command, argv: list[str]) -> None:
        if self.ctx.run(argv, owner=self):
            self.card.set_running(True)

    def on_busy(self, busy: bool) -> None:
        super().on_busy(busy)
        if not busy:
            self.card.set_running(False)

    def apply_values(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.card.form.set_value(key, value)


# ------------------------------------------------------------------- dashboard
class DashboardPage(Page):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("Pipeline")
        title.setObjectName("PageTitle")
        self.subtitle = muted("reading status…")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        head.addWidget(title)
        head.addWidget(self.subtitle, 1)
        head.addWidget(refresh)
        layout.addLayout(head)

        self.cards_host = QWidget()
        self.cards = QGridLayout(self.cards_host)
        self.cards.setContentsMargins(0, 0, 0, 0)
        self.cards.setHorizontalSpacing(10)
        self.cards.setVerticalSpacing(10)
        for column in range(4):
            self.cards.setColumnStretch(column, 1)
        layout.addWidget(self.cards_host)

        self.strip = muted("")
        layout.addWidget(self.strip)

        self.doctor_table = TablePane("doctor")
        self.runs_table = TablePane("runs")
        tabs = QTabWidget()
        tabs.addTab(self.doctor_table, "Doctor")
        tabs.addTab(self.runs_table, "Run ledger")
        layout.addWidget(tabs, 1)

        self._doctor_kpi: Kpi | None = None
        self._status: dict[str, Any] = {}

    def refresh(self) -> None:
        super().refresh()
        if self.ctx.run(["status", "--json"], json_flag=True, owner=self):
            self.ctx.notify("Reading pipeline status…", "info")

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if not result.payload:
            self.ctx.notify(f"{result.command_line} returned no JSON", "warn")
            return
        stage = result.argv[0]
        if stage == "status":
            self._status = result.payload
            self._fill_cards()
            self.runs_table.set_rows(run_rows(result.payload))
            database = str(result.payload.get("database") or "")
            self.subtitle.setText(f"{database} · refreshed {time.strftime('%H:%M:%S')}")
            self.ctx.run(["doctor", "--json"], json_flag=True, owner=self)
        elif stage == "doctor":
            self._doctor_kpi = doctor_headline(result.payload)
            self._fill_cards()
            self.doctor_table.set_rows(doctor_rows(result.payload))

    def _fill_cards(self) -> None:
        _clear_layout(self.cards)
        cards = status_kpis(self._status)
        if self._doctor_kpi is not None:
            cards.insert(0, self._doctor_kpi)
        for index, kpi in enumerate(cards):
            self.cards.addWidget(
                KpiCard(kpi.label, kpi.value, kpi.tone, kpi.hint), index // 4, index % 4
            )
        self.strip.setText(status_strip(self._status))


# ---------------------------------------------------------------------- leads
class LeadsPage(Page):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._leads_tab(), "Leads")
        self.tabs.addTab(self._contacts_tab(), "Contacts")
        self.tabs.currentChanged.connect(lambda _index: self.refresh())
        layout.addWidget(self.tabs, 1)

    # ------------------------------------------------------------------ tabs
    def _leads_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem("any status", None)
        for status in LEAD_STATUSES:
            self.status_filter.addItem(status, status)
        self.source_filter = QComboBox()
        self.source_filter.addItem("any source", None)
        for source in SOURCES:
            self.source_filter.addItem(source, source)
        self.lead_limit = QSpinBox()
        self.lead_limit.setRange(1, 5000)
        self.lead_limit.setValue(200)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.refresh("leads"))
        bar.addWidget(QLabel("Status"))
        bar.addWidget(self.status_filter)
        bar.addWidget(QLabel("Source"))
        bar.addWidget(self.source_filter)
        bar.addWidget(QLabel("Rows"))
        bar.addWidget(self.lead_limit)
        bar.addStretch(1)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        actions = QHBoxLayout()
        for text, slot in (
            ("Enrich…", self._enrich),
            ("Compose…", self._compose),
            ("Approve", self._approve),
            ("Unapprove", self._unapprove),
            ("Mark…", self._mark),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.leads_table = TablePane("leads")
        self.leads_table.row_activated.connect(lambda _row: self._enrich())
        layout.addWidget(self.leads_table, 1)
        return tab

    def _contacts_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        self.contact_limit = QSpinBox()
        self.contact_limit.setRange(1, 5000)
        self.contact_limit.setValue(300)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.refresh("contacts"))
        bar.addWidget(QLabel("Rows"))
        bar.addWidget(self.contact_limit)
        bar.addStretch(1)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        self.contacts_table = TablePane("contacts")
        layout.addWidget(self.contacts_table, 1)
        return tab

    # --------------------------------------------------------------- refresh
    def refresh(self, which: str | None = None) -> None:
        super().refresh()
        which = which or ("leads" if self.tabs.currentIndex() == 0 else "contacts")
        if which == "leads":
            argv = ["leads", "--json", "--limit", str(self.lead_limit.value())]
            status = self.status_filter.currentData()
            source = self.source_filter.currentData()
            if status:
                argv += ["--status", str(status)]
            if source:
                argv += ["--source", str(source)]
            self.ctx.run(argv, json_flag=True, owner=self)
        else:
            self.ctx.run(
                ["contacts", "--json", "--limit", str(self.contact_limit.value())],
                json_flag=True,
                owner=self,
            )

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if not result.payload:
            self.refresh()
            return
        if result.argv[0] == "leads":
            self.leads_table.set_rows(rows_of(result.payload))
        elif result.argv[0] == "contacts":
            self.contacts_table.set_rows(rows_of(result.payload))

    # --------------------------------------------------------------- actions
    def _selected(self) -> dict[str, Any] | None:
        row = self.leads_table.current_row()
        if row is None:
            self.ctx.notify("Select a lead first.", "warn")
        return row

    def _enrich(self) -> None:
        row = self._selected()
        if row:
            self.ctx.navigate("enrich", {"lead_id": row["id"]})

    def _compose(self) -> None:
        row = self._selected()
        if row:
            self.ctx.navigate("compose", {"lead_id": row["id"]})

    def _approve(self) -> None:
        row = self._selected()
        if row:
            self.confirm_run(command("approve"), {"lead_id": row["id"]})

    def _unapprove(self) -> None:
        row = self._selected()
        if row:
            self.confirm_run(command("unapprove"), {"lead_id": row["id"]})

    def _mark(self) -> None:
        row = self._selected()
        if not row:
            return
        dialog = MarkDialog("Mark lead outcome", ("replied", "bounced", "sent", "suppressed"), self)
        if dialog.exec_():
            status, reason = dialog.values()
            values: dict[str, Any] = {"lead_id": row["id"], "status": status}
            if reason:
                values["reason"] = reason
            self.confirm_run(command("mark"), values)


# --------------------------------------------------------------------- drafts
class DraftsPage(Page):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        self.limit = QSpinBox()
        self.limit.setRange(1, 2000)
        self.limit.setValue(200)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(QLabel("Rows"))
        bar.addWidget(self.limit)
        bar.addStretch(1)
        for text, slot in (
            ("Approve", self._approve),
            ("Unapprove", self._unapprove),
            ("Suppress email", self._suppress),
            ("Copy body", self._copy_body),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        self.table = TablePane("drafts")
        self.detail = QPlainTextEdit()
        self.detail.setObjectName("Draft")
        self.detail.setReadOnly(True)
        splitter.addWidget(self.table)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
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
        if result.payload:
            self.table.set_rows(rows_of(result.payload))
        else:
            self.refresh()

    def _show_detail(self, row: object) -> None:
        if not isinstance(row, dict):
            self.detail.clear()
            return
        header = (
            f"lead {row.get('lead_id')} · message {row.get('message_id')} · "
            f"{row.get('contact')} · {row.get('lead_status')}\n"
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


# -------------------------------------------------------------------- sources
class SourcesPage(QWidget):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Sources")
        title.setObjectName("PageTitle")
        head.addWidget(title)
        head.addWidget(muted("Marketplaces are a lead source, not a contact source."), 1)
        layout.addLayout(head)

        cards = QWidget()
        cards_layout = QVBoxLayout(cards)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(10)
        for spec in COMMANDS:
            if spec.id in {"source_shopify", "source_olx"}:
                card = CommandCard(spec)
                card.run_requested.connect(lambda _spec, argv: ctx.run(argv, owner=self))
                cards_layout.addWidget(card)
        cards_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(cards)
        layout.addWidget(scroll, 1)

    def on_busy(self, busy: bool) -> None:
        for button in self.findChildren(QPushButton):
            button.setEnabled(not busy)
        for card in self.findChildren(CommandCard):
            card.set_running(busy)


# ------------------------------------------------------------------ enrichment
class EnrichPage(CommandPage):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        note = QFrame()
        note_layout = QVBoxLayout(note)
        note_layout.setContentsMargins(0, 0, 0, 0)
        note_layout.addWidget(
            muted(
                "Run with 'Resolve domains only' first to review OLX domains: it writes "
                "enrichments.domain and leaves lead status untouched. Rerunning a "
                "completed lead is a no-op unless you tick Force."
            )
        )
        super().__init__(ctx, "enrich", parent, extra=note)


# --------------------------------------------------------------------- compose
class ComposePage(CommandPage):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        review = QPushButton("Review drafts →")
        review.setObjectName("Primary")
        review.clicked.connect(lambda: ctx.navigate("drafts"))
        layout.addWidget(
            muted(
                "Compose needs the four sender fields in .env and a running Ollama. "
                "Drafts are validated before they are stored; failures never downgrade "
                "an existing draft."
            ),
            1,
        )
        layout.addWidget(review)
        super().__init__(ctx, "compose", parent, extra=row)


# ---------------------------------------------------------------------- mailer
class MailerPage(CommandPage):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        policy = QWidget()
        layout = QHBoxLayout(policy)
        layout.setContentsMargins(0, 0, 0, 0)
        self.policy_label = muted("reading policy…")
        refresh = QPushButton("Refresh policy")
        refresh.clicked.connect(self.refresh)
        layout.addWidget(self.policy_label, 1)
        layout.addWidget(refresh)
        super().__init__(ctx, "send", parent, extra=policy)

    def refresh(self) -> None:
        super().refresh()
        self.ctx.run(["status", "--json"], json_flag=True, owner=self)

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if not result.payload or result.argv[0] != "status":
            return
        payload = result.payload
        kill_switch = payload.get("kill_switch")
        window = payload.get("window") or {}
        cap = window.get("daily_cap")
        self.policy_label.setText(
            f"kill switch: {kill_switch} · window: {window_text(window)}"
            + (f" · daily cap {cap}" if cap is not None else "")
            + " · dry run writes .eml to data/outbox"
        )


# ---------------------------------------------------------------------- ledger
class LedgerPage(Page):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        title = QLabel("Ledger")
        title.setObjectName("PageTitle")
        bar.addWidget(title)
        bar.addStretch(1)
        resolve = QPushButton("Resolve send…")
        resolve.clicked.connect(self._resolve)
        open_file = QPushButton("Open .eml")
        open_file.clicked.connect(self._open_eml)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(resolve)
        bar.addWidget(open_file)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Vertical)
        self.sends_table = TablePane("sends")
        self.outbox_table = TablePane("outbox")
        splitter.addWidget(self.sends_table)
        splitter.addWidget(self.outbox_table)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    def refresh(self) -> None:
        super().refresh()
        self.ctx.run(["sends", "--json", "--limit", "200"], json_flag=True, owner=self)

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if not result.payload:
            self.refresh()
            return
        if result.argv[0] == "sends":
            self.sends_table.set_rows(rows_of(result.payload))
            self.ctx.run(["outbox", "--json", "--limit", "200"], json_flag=True, owner=self)
        elif result.argv[0] == "outbox":
            self.outbox_table.set_rows(rows_of(result.payload))

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


# ----------------------------------------------------------------------- inbox
class InboxPage(Page):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Inbox")
        title.setObjectName("PageTitle")
        head.addWidget(title)
        head.addWidget(
            muted("Read-only IMAP: EXAMINE + BODY.PEEK. Nothing is marked seen or moved."), 1
        )
        layout.addLayout(head)

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
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        self.table = TablePane("inbound")
        layout.addWidget(self.table, 1)

    def refresh(self) -> None:
        super().refresh()
        self.ctx.run(["inbox", "list", "--json", "--limit", "200"], json_flag=True, owner=self)

    def on_result(self, result: Result) -> None:
        super().on_result(result)
        if result.argv[:2] == ["inbox", "list"]:
            self.table.set_rows(rows_of(result.payload))
        elif not result.payload:
            self.refresh()

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


# ------------------------------------------------------------------- operations
class OpsPage(QWidget):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Operations")
        title.setObjectName("PageTitle")
        head.addWidget(title)
        head.addWidget(muted("Maintenance, audit and manual overrides."), 1)
        layout.addLayout(head)

        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        order = (
            "db_init",
            "doctor",
            "backup",
            "mark",
            "suppress",
            "unsuppress",
            "stats",
            "version",
        )
        for index, spec_id in enumerate(order):
            card = CommandCard(command(spec_id))
            card.run_requested.connect(lambda _spec, argv: ctx.run(argv, owner=self))
            grid.addWidget(card, index // 2, index % 2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

    def on_busy(self, busy: bool) -> None:
        for button in self.findChildren(QPushButton):
            button.setEnabled(not busy)
        for card in self.findChildren(CommandCard):
            card.set_running(busy)


# ------------------------------------------------------------------ main window
NAV_PAGES: tuple[tuple[str, str], ...] = (
    ("dashboard", "Dashboard"),
    ("leads", "Prospects"),
    ("drafts", "Drafts"),
    ("sources", "Sources"),
    ("enrich", "Enrichment"),
    ("compose", "Compose"),
    ("mailer", "Mailer"),
    ("ledger", "Ledger"),
    ("inbox", "Inbox"),
    ("ops", "Operations"),
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

        self.ctx = Ctx(run=self.run_stage, navigate=self.navigate, notify=self.notify)
        self._owner: Page | None = None

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._header())

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(12, 12, 12, 12)
        body_layout.setSpacing(12)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(186)
        self.stack = QStackedWidget()

        self.pages: dict[str, Any] = {
            "dashboard": DashboardPage(self.ctx),
            "leads": LeadsPage(self.ctx),
            "drafts": DraftsPage(self.ctx),
            "sources": SourcesPage(self.ctx),
            "enrich": EnrichPage(self.ctx),
            "compose": ComposePage(self.ctx),
            "mailer": MailerPage(self.ctx),
            "ledger": LedgerPage(self.ctx),
            "inbox": InboxPage(self.ctx),
            "ops": OpsPage(self.ctx),
        }
        for page_id, label in NAV_PAGES:
            self.nav.addItem(label)
            self.stack.addWidget(self.pages[page_id])
        self._page_ids = [page_id for page_id, _label in NAV_PAGES]

        body_layout.addWidget(self.nav)
        body_layout.addWidget(self.stack, 1)
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
        self.resizeDocks([dock], [230], Qt.Vertical)

        self.nav.currentRowChanged.connect(self._on_nav)
        self.nav.setCurrentRow(0)
        self.statusBar().showMessage("Ready")

        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start()

        QTimer.singleShot(200, self.pages["dashboard"].refresh)

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

    # ------------------------------------------------------------------- runs
    def run_stage(
        self,
        argv: list[str],
        *,
        json_flag: bool = False,
        owner: Page | None = None,
    ) -> bool:
        if self.runner.busy:
            self.notify("A stage is already running — wait or press Stop.", "warn")
            return False
        self._owner = owner
        self.console.begin("forgesmith " + " ".join(argv))
        self._set_busy(True)
        started = self.runner.start(argv, json_flag=json_flag)
        if not started:
            self._set_busy(False)
            self.notify("Could not start the stage.", "bad")
        else:
            note = getattr(owner, "note_loaded", None)
            if callable(note):
                note()
        return started

    def _on_started(self, command_line: str) -> None:
        self.statusBar().showMessage(f"Running {command_line}…")

    def _on_chunk(self, text: str, channel: str) -> None:
        self.console.append(text, channel)

    def _on_finished(self, result: Result) -> None:
        self.console.finish(result)
        owner, self._owner = self._owner, None
        self._set_busy(False)

        if owner is not None and hasattr(owner, "on_result"):
            try:
                owner.on_result(result)
            except Exception as exc:  # a page bug must not kill the console
                self.console.append(f"\n[gui] {type(exc).__name__}: {exc}\n", "err")

        if not self.runner.busy:
            self._set_busy(False)
            label, tone = exit_tone(result.exit_code)
            self.notify(f"{result.command_line} → {label} ({result.duration_s}s)", tone)
            current = self.stack.currentWidget()
            for page in self.pages.values():
                if page is not owner and page is not current and hasattr(page, "mark_stale"):
                    page.mark_stale()
            # A page opened while another stage was running had its refresh
            # refused, so it would sit on its placeholder until F5. Pick it up
            # now that the runner is free.
            if current is not owner and getattr(current, "is_stale", lambda: False)():
                self._safe(current.refresh)

    def _stop(self) -> None:
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
        self.nav.setCurrentRow(self._page_ids.index(page_id))
        page = self.pages[page_id]
        if values and hasattr(page, "apply_values"):
            page.apply_values(values)

    def _on_nav(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        page = self.stack.currentWidget()
        if hasattr(page, "is_stale") and page.is_stale():
            self._safe(page.refresh)

    def _refresh_current(self) -> None:
        page = self.stack.currentWidget()
        if hasattr(page, "refresh"):
            self._safe(page.refresh)

    def _auto_refresh(self) -> None:
        if self.runner.busy:
            return
        if self.stack.currentWidget() is self.pages["dashboard"]:
            self._safe(self.pages["dashboard"].refresh)

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
