"""Reusable widgets: KPI cards, spec-driven forms, console, sortable tables."""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedLayout,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from forgesmith.gui.commands import (
    TABLES,
    Command,
    Field,
    MissingFields,
    build_argv,
    confirmation,
    exit_tone,
    is_dangerous,
    status_tone,
)
from forgesmith.gui.payload import cell_text

TONE_COLORS = {
    "ok": "#15803d",
    "warn": "#b45309",
    "bad": "#b91c1c",
    "info": "#1d4ed8",
    "muted": "#6b7280",
}


def tone_color(tone: str) -> str:
    return TONE_COLORS.get(tone, TONE_COLORS["muted"])


def muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Hint")
    label.setWordWrap(True)
    return label


class KpiCard(QFrame):
    """One dashboard number with a label, a tone and an optional hint."""

    def __init__(self, label: str, value: str, tone: str = "muted", hint: str = "") -> None:
        super().__init__()
        self.setObjectName("Kpi")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        caption = QLabel(label.upper())
        caption.setObjectName("KpiLabel")
        self.value = QLabel(value)
        self.value.setObjectName("KpiValue")
        self.value.setWordWrap(True)
        if len(value) > 16:
            # Long values (a window timestamp, a status breakdown) would push
            # the card into three lines; step the type down instead.
            self.value.setStyleSheet(f"color: {tone_color(tone)}; font-size: 10pt;")
        else:
            self.value.setStyleSheet(f"color: {tone_color(tone)};")
        layout.addWidget(caption)
        layout.addWidget(self.value)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("KpiHint")
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)
        layout.addStretch(1)


class FormWidget(QWidget):
    """Builds its controls from a :class:`Command` spec."""

    changed = pyqtSignal()

    def __init__(self, spec: Command, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._editors: dict[str, QWidget] = {}

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(8)
        layout.setColumnStretch(1, 1)

        row = 0
        for field in spec.fields:
            editor = self._make_editor(field)
            if field.kind == "bool":
                layout.addWidget(editor, row, 0, 1, 2)
            else:
                label = QLabel(field.label)
                label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if field.help:
                    label.setToolTip(field.help)
                layout.addWidget(label, row, 0)
                layout.addWidget(editor, row, 1)
            row += 1

    # ------------------------------------------------------------ editors
    def _make_editor(self, field: Field) -> QWidget:
        if field.kind == "bool":
            box = QCheckBox(field.label)
            box.setChecked(bool(field.default))
            if field.help:
                box.setToolTip(field.help)
            box.toggled.connect(self.changed)
            self._editors[field.key] = box
            return box

        if field.kind == "choice":
            combo = QComboBox()
            if not field.required:
                combo.addItem("any", None)
            for choice in field.choices:
                combo.addItem(choice, choice)
            if field.default:
                index = combo.findData(field.default)
                if index >= 0:
                    combo.setCurrentIndex(index)
            combo.currentIndexChanged.connect(self.changed)
            self._editors[field.key] = combo
            return combo

        if field.kind == "path":
            holder = QWidget()
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(4)
            line = QLineEdit(str(field.default or ""))
            line.setPlaceholderText(field.placeholder)
            browse = QToolButton()
            browse.setText("…")
            browse.setPopupMode(QToolButton.InstantPopup)
            menu = QMenu(browse)
            menu.addAction("Choose directory…", lambda: self._pick_dir(line))
            menu.addAction("Choose file…", lambda: self._pick_file(line))
            browse.setMenu(menu)
            layout.addWidget(line, 1)
            layout.addWidget(browse)
            line.textChanged.connect(self.changed)
            self._editors[field.key] = line
            return holder

        line = QLineEdit()
        if field.kind == "multi":
            default = field.default
            if isinstance(default, (list, tuple)):
                line.setText(", ".join(str(item) for item in default))
            elif default:
                line.setText(str(default))
            line.setPlaceholderText(field.placeholder or "comma-separated")
        else:
            if field.default is not None:
                line.setText(str(field.default))
            line.setPlaceholderText(field.placeholder)
        if field.help:
            line.setToolTip(field.help)
        line.textChanged.connect(self.changed)
        self._editors[field.key] = line
        return line

    def _pick_dir(self, line: QLineEdit) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose directory", line.text() or ".")
        if chosen:
            line.setText(chosen)

    def _pick_file(self, line: QLineEdit) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose file", line.text() or ".")
        if chosen:
            line.setText(chosen)

    # ------------------------------------------------------------- values
    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in self._spec.fields:
            editor = self._editors[field.key]
            if field.kind == "bool":
                out[field.key] = editor.isChecked()  # type: ignore[attr-defined]
            elif field.kind == "choice":
                out[field.key] = editor.currentData()  # type: ignore[attr-defined]
            elif field.kind == "multi":
                text = editor.text().strip()  # type: ignore[attr-defined]
                items = [part.strip() for part in text.split(",") if part.strip()]
                out[field.key] = items or None
            else:
                text = editor.text().strip()  # type: ignore[attr-defined]
                out[field.key] = text or None
        return out

    def set_value(self, key: str, value: Any) -> None:
        editor = self._editors.get(key)
        if editor is None:
            return
        field = next((item for item in self._spec.fields if item.key == key), None)
        if field is None:
            return
        if field.kind == "bool":
            editor.setChecked(bool(value))  # type: ignore[attr-defined]
        elif field.kind == "choice":
            index = editor.findData(value)  # type: ignore[attr-defined]
            if index >= 0:
                editor.setCurrentIndex(index)  # type: ignore[attr-defined]
        elif field.kind == "multi":
            items = value if isinstance(value, (list, tuple)) else [value]
            editor.setText(", ".join(str(item) for item in items))  # type: ignore[attr-defined]
        else:
            editor.setText("" if value is None else str(value))  # type: ignore[attr-defined]


class CommandCard(QGroupBox):
    """A form plus a Run button, with the confirmation the CLI would ask for."""

    run_requested = pyqtSignal(object, object)  # spec, argv

    def __init__(self, spec: Command, parent: QWidget | None = None) -> None:
        super().__init__(spec.label, parent)
        self._spec = spec

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        if spec.summary:
            layout.addWidget(muted(spec.summary))

        self.notice = QLabel(spec.notice or "")
        self.notice.setObjectName("Notice")
        self.notice.setWordWrap(True)
        self.notice.setVisible(bool(spec.notice))
        layout.addWidget(self.notice)

        self.form = FormWidget(spec)
        self.form.setVisible(bool(spec.fields))
        layout.addWidget(self.form)

        self.error = QLabel("")
        self.error.setObjectName("Error")
        self.error.setWordWrap(True)
        self.error.setVisible(False)
        layout.addWidget(self.error)

        # Keep the form tight against its summary; the spare height of a grid
        # row belongs between the form and the Run button, not inside it.
        layout.addStretch(1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.button = QPushButton("Run")
        self.button.setObjectName("Primary")
        self.button.setMinimumWidth(120)
        self.button.clicked.connect(self._submit)
        footer.addWidget(self.button)
        layout.addLayout(footer)

        self.form.changed.connect(self._refresh_notice)
        self._refresh_notice()

    def _refresh_notice(self) -> None:
        if not self._spec.notice:
            return
        values = self.form.values()
        if is_dangerous(self._spec, values):
            self.notice.setObjectName("DangerNotice")
            self.notice.setText("LIVE MODE — this run delivers real email.")
        else:
            self.notice.setObjectName("Notice")
            self.notice.setText(self._spec.notice)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)

    def _submit(self) -> None:
        values = self.form.values()
        try:
            argv = build_argv(self._spec, values)
        except MissingFields as exc:
            self.error.setText("Missing or invalid: " + "; ".join(exc.labels))
            self.error.setVisible(True)
            return

        prompt = confirmation(self._spec, values)
        if prompt and not self._confirm(prompt):
            return

        self.error.setVisible(False)
        self.run_requested.emit(self._spec, argv)

    def _confirm(self, text: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(self._spec.label)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.button(QMessageBox.Yes).setText("Run it")
        return box.exec_() == QMessageBox.Yes

    def set_running(self, running: bool) -> None:
        self.button.setEnabled(not running)
        self.button.setText("Running…" if running else "Run")


class ConsolePane(QWidget):
    """Streamed stdout/stderr plus the exit badge of the last stage."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setObjectName("ConsoleHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 6, 10, 6)
        header_layout.setSpacing(8)

        title = QLabel("Console")
        title.setObjectName("ConsoleTitle")
        self.command_label = QLabel("no command yet")
        self.command_label.setObjectName("ConsoleCommand")
        self.badge = QLabel("idle")
        self.badge.setObjectName("Badge")
        self.badge.setStyleSheet(f"color: {tone_color('muted')};")

        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        clear.clicked.connect(self.clear)
        copy = QPushButton("Copy")
        copy.setObjectName("Ghost")
        copy.clicked.connect(self._copy)
        save = QPushButton("Save…")
        save.setObjectName("Ghost")
        save.clicked.connect(self._save)

        header_layout.addWidget(title)
        header_layout.addWidget(self.command_label, 1)
        header_layout.addWidget(self.badge)
        header_layout.addWidget(clear)
        header_layout.addWidget(copy)
        header_layout.addWidget(save)

        self.edit = QPlainTextEdit()
        self.edit.setObjectName("Console")
        self.edit.setReadOnly(True)
        self.edit.setMaximumBlockCount(10_000)
        self.edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        self.edit.setFont(font)
        self._follow = True
        self.edit.verticalScrollBar().valueChanged.connect(self._on_scroll)

        layout.addWidget(header)
        layout.addWidget(self.edit, 1)

    def _on_scroll(self, value: int) -> None:
        bar = self.edit.verticalScrollBar()
        self._follow = value >= bar.maximum() - 4

    def _append(self, text: str, color: str | None = None) -> None:
        cursor = self.edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(color))
        cursor.insertText(text, fmt)
        if self._follow:
            self.edit.verticalScrollBar().setValue(self.edit.verticalScrollBar().maximum())

    def begin(self, command_line: str) -> None:
        self.command_label.setText(command_line)
        self.badge.setText("running")
        self.badge.setStyleSheet(f"color: {tone_color('info')};")
        self._append(f"\n$ {command_line}\n", "#93c5fd")

    def append(self, text: str, channel: str = "out") -> None:
        self._append(text, "#94a3b8" if channel == "err" else "#e2e8f0")

    def finish(self, result: object) -> None:
        label, tone = exit_tone(result.exit_code)  # type: ignore[attr-defined]
        self.badge.setText(f"{label} · {result.duration_s}s")  # type: ignore[attr-defined]
        self.badge.setStyleSheet(f"color: {tone_color(tone)}; font-weight: 600;")
        self._append(
            f"\n— exit {result.exit_code} · {result.duration_s}s · {label}\n",  # type: ignore[attr-defined]
            tone_color(tone),
        )

    def set_idle(self) -> None:
        self.badge.setText("idle")
        self.badge.setStyleSheet(f"color: {tone_color('muted')};")

    def clear(self) -> None:
        self.edit.clear()

    def text(self) -> str:
        return self.edit.toPlainText()

    def _copy(self) -> None:
        from PyQt5.QtWidgets import QApplication

        QApplication.clipboard().setText(self.text())

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save console log", "forgesmith-gui.log")
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.text())


class _SortItem(QTableWidgetItem):
    """A cell that displays formatted text but sorts on its real value.

    ``QTableWidgetItem.setData(Qt.DisplayRole, 51)`` is a no-op when the cell
    already reads ``"51"`` (Qt compares the variants and finds them equal), so
    ids and counts would keep sorting as strings — ``9`` before ``51``. Sorting
    on a separate key keeps both the formatting and the ordering honest.
    """

    __slots__ = ("_sort_key",)

    def __init__(self, text: str, sort_key: object) -> None:
        super().__init__(text)
        self._sort_key = sort_key

    def __lt__(self, other: object) -> bool:
        if isinstance(other, _SortItem):
            left, right = self._sort_key, other._sort_key
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left < right
            return str(left) < str(right)
        return super().__lt__(other)  # type: ignore[arg-type]


def _sort_key(value: object) -> object:
    """Numbers sort numerically; everything else sorts case-insensitively."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return str(value or "").lower()


PLACEHOLDERS: dict[str, str] = {
    "leads": "No leads yet — run a source stage to populate the database.",
    "contacts": "No contacts yet — run Enrichment on a lead with a storefront.",
    "drafts": "No drafts yet — run Compose to generate grounded pitches.",
    "sends": "No send attempts yet — a dry run writes .eml files to the outbox.",
    "outbox": "No .eml evidence yet — run the Mailer in dry-run mode.",
    "inbound": "No inbound mail yet — run Sync inbox.",
    "doctor": "No checks yet — press Refresh.",
    "runs": "No stage runs recorded yet.",
}


class TablePane(QWidget):
    """A sortable read-only table fed by ``--json`` rows."""

    row_selected = pyqtSignal(object)  # dict | None
    row_activated = pyqtSignal(object)  # dict

    def __init__(self, table_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.columns = TABLES[table_key]
        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels([column.label for column in self.columns])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        for index, column in enumerate(self.columns):
            self.table.setColumnWidth(index, column.width)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._emit_selection)
        self.table.itemDoubleClicked.connect(self._emit_activation)

        self.placeholder = QLabel(PLACEHOLDERS.get(table_key, "No rows yet — press Refresh."))
        self.placeholder.setObjectName("Placeholder")
        self.placeholder.setAlignment(Qt.AlignCenter)

        self._stack = QStackedLayout(self)
        self._stack.addWidget(self.placeholder)
        self._stack.addWidget(self.table)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, column in enumerate(self.columns):
                value = row.get(column.key)
                item = _SortItem(cell_text(value), _sort_key(value))
                if column.align == "right":
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                elif column.align == "center":
                    item.setTextAlignment(Qt.AlignCenter)
                item.setToolTip(str(value or ""))
                if column.key == "status":
                    item.setForeground(QColor(tone_color(status_tone(value))))
                elif column.key == "exit_code" and value is not None:
                    _label, tone = exit_tone(int(value))
                    item.setForeground(QColor(tone_color(tone)))
                if column_index == 0:
                    item.setData(Qt.UserRole, row)
                self.table.setItem(row_index, column_index, item)
        self.table.setSortingEnabled(True)
        self._stack.setCurrentIndex(1 if rows else 0)
        if rows:
            self.table.selectRow(0)
        else:
            self.row_selected.emit(None)

    def current_row(self) -> dict[str, Any] | None:
        selection = self.table.selectionModel()
        if selection is None or not selection.hasSelection():
            return None
        row_index = selection.selectedRows()[0].row()
        item = self.table.item(row_index, 0)
        return item.data(Qt.UserRole) if item else None

    def _emit_selection(self) -> None:
        self.row_selected.emit(self.current_row())

    def _emit_activation(self, _item: object) -> None:
        row = self.current_row()
        if row:
            self.row_activated.emit(row)


class MarkDialog(QDialog):
    """Collect a status plus an audited reason."""

    def __init__(
        self,
        title: str,
        statuses: tuple[str, ...],
        parent: QWidget | None = None,
        reason_label: str = "Reason",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(360)

        layout = QFormLayout(self)
        self.status = QComboBox()
        self.status.addItems(statuses)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("recorded in the audit trail")
        layout.addRow("Status", self.status)
        layout.addRow(reason_label, self.reason)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self) -> tuple[str, str]:
        return self.status.currentText(), self.reason.text().strip()
