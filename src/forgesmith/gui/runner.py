"""Run one CLI stage at a time and stream it into the GUI.

A subprocess, not an in-process call: a stage can block for minutes, crash, or
be cancelled, and the GUI must stay responsive and honest about what happened.
It also means the desktop app inherits every gate the command line enforces.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any

from PyQt5.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

from forgesmith.config import PROJECT_ROOT
from forgesmith.gui.payload import parse_payload


@dataclass
class Result:
    argv: list[str]
    exit_code: int
    duration_s: float
    stdout: str
    stderr: str
    payload: dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False

    @property
    def command_line(self) -> str:
        return "forgesmith " + " ".join(self.argv)

    @property
    def failed(self) -> bool:
        return self.exit_code != 0


class CliRunner(QObject):
    """Owns exactly one ``python -m forgesmith`` process."""

    started = pyqtSignal(str)  # command line
    chunk = pyqtSignal(str, str)  # text, channel ("out" | "err")
    finished = pyqtSignal(object)  # Result

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.SeparateChannels)
        self._process.setWorkingDirectory(str(PROJECT_ROOT))
        environment = QProcessEnvironment.systemEnvironment()
        # The CLI already forces UTF-8, but the pipe still needs to agree.
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("NO_COLOR", "1")
        environment.insert("TERM", "dumb")
        environment.insert("COLUMNS", "110")
        environment.insert("FORGESMITH_GUI", "1")
        self._process.setProcessEnvironment(environment)

        self._process.readyReadStandardOutput.connect(self._read_stdout)
        self._process.readyReadStandardError.connect(self._read_stderr)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_error)

        self._argv: list[str] = []
        self._json = False
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._started_at = 0.0
        self._cancelled = False
        self._running = False

    # ------------------------------------------------------------- state
    @property
    def busy(self) -> bool:
        return self._running

    @property
    def argv(self) -> list[str]:
        return list(self._argv)

    # ------------------------------------------------------------- control
    def start(self, argv: list[str], *, json_flag: bool = False) -> bool:
        if self._running:
            return False
        self._argv = list(argv)
        self._json = json_flag
        self._stdout = []
        self._stderr = []
        self._cancelled = False
        self._started_at = time.monotonic()
        self._running = True

        self._process.setProgram(sys.executable)
        self._process.setArguments(["-m", "forgesmith", *argv])
        self.started.emit("forgesmith " + " ".join(argv))
        if json_flag:
            self.chunk.emit("(machine-readable run: rows go to the table above)\n", "err")
        self._process.start()
        return True

    def cancel(self) -> None:
        if not self._running:
            return
        self._cancelled = True
        self.chunk.emit("\n[cancelled by operator]\n", "err")
        self._process.kill()

    # ------------------------------------------------------------- signals
    def _read_stdout(self) -> None:
        text = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        if not text:
            return
        self._stdout.append(text)
        # A ``--json`` run's stdout is a payload for the table, not for a human:
        # streaming it would bury the console in raw braces. Keep stderr live.
        if not self._json:
            self.chunk.emit(text, "out")

    def _read_stderr(self) -> None:
        text = bytes(self._process.readAllStandardError()).decode("utf-8", "replace")
        if text:
            self._stderr.append(text)
            self.chunk.emit(text, "err")

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.FailedToStart:
            message = f"could not start {sys.executable} -m forgesmith: {self._process.errorString()}"
            self._stderr.append(message)
            self.chunk.emit(message + "\n", "err")
            self._finish(exit_code=127)

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        # Flush anything the process wrote just before exiting.
        self._read_stdout()
        self._read_stderr()
        self._finish(exit_code)

    def _finish(self, exit_code: int) -> None:
        if not self._running:
            return
        self._running = False
        stdout = "".join(self._stdout)
        payload = parse_payload(stdout) if self._json else {}
        if self._json and not payload and stdout.strip():
            # Nothing parsed, so the table stays empty: show the raw text rather
            # than leaving the operator with a silent, blank result.
            self.chunk.emit("\n[unparsed stdout]\n" + stdout + "\n", "err")
        result = Result(
            argv=self._argv,
            exit_code=exit_code,
            duration_s=round(time.monotonic() - self._started_at, 2),
            stdout=stdout,
            stderr="".join(self._stderr),
            payload=payload,
            cancelled=self._cancelled,
        )
        self.finished.emit(result)
