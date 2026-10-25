"""Pure helpers that turn CLI JSON into the rows the GUI renders.

Kept apart from the widgets so the shape of the dashboard can be tested without
a display server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Kpi:
    label: str
    value: str
    tone: str = "muted"
    hint: str = ""


def parse_payload(text: str) -> dict[str, Any]:
    """Parse a ``--json`` payload, tolerating stray text before the object.

    ``status``/``doctor`` write JSON to stdout, but a stage can still emit a
    warning line; slicing from the first brace keeps the GUI resilient.
    """
    stripped = (text or "").strip()
    if not stripped:
        return {}
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            payload = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def rows_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("rows")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def human_bytes(count: object) -> str:
    try:
        size = float(count or 0)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def window_text(window: dict[str, Any]) -> str:
    if not isinstance(window, dict) or not window:
        return "unknown"
    if window.get("open") is None:
        return f"unavailable ({window.get('error', 'policy error')})"
    state = "open" if window.get("open") else "closed"
    opening = _short_stamp(window.get("next_opening"))
    if not window.get("open") and opening:
        return f"{state} · {opening}"
    return state


def _short_stamp(value: object) -> str:
    """``2026-10-26T08:00:00+00:00`` -> ``26 Oct 08:00 UTC``."""
    text = str(value or "")
    if not text:
        return ""
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return text
    suffix = "UTC" if stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0 else ""
    return (stamp.strftime("%d %b %H:%M") + (" " + suffix if suffix else "")).strip()


def status_kpis(payload: dict[str, Any]) -> list[Kpi]:
    """Headline cards for the dashboard (the doctor card is added by the page)."""
    if not payload:
        return [Kpi("status", "no data", "muted", "run `status --json`")]

    leads = payload.get("leads") or {}
    by_status = dict(leads.get("by_status") or {})
    by_source = dict(leads.get("by_source") or {})
    sends = dict(payload.get("sends") or {})
    outbox = payload.get("outbox") or {}
    uncertain = int(payload.get("uncertain") or 0)
    kill_switch = str(payload.get("kill_switch") or "-")
    composed = int(by_status.get("composed", 0))
    queued = int(by_status.get("queued", 0))

    return [
        Kpi(
            "leads",
            str(leads.get("total", 0)),
            "info",
            " · ".join(f"{name} {count}" for name, count in sorted(by_source.items())) or "-",
        ),
        Kpi(
            "awaiting review",
            str(composed),
            "warn" if composed else "muted",
            "composed — approve to queue",
        ),
        Kpi(
            "queued",
            str(queued),
            "warn" if queued else "muted",
            "eligible for send",
        ),
        Kpi(
            "sends",
            " · ".join(f"{name} {count}" for name, count in sorted(sends.items())) or "none",
            "muted",
            f"{uncertain} uncertain outstanding" if uncertain else "none outstanding",
        ),
        Kpi(
            "kill switch",
            kill_switch,
            "warn" if kill_switch == "armed" else "bad",
            "FORGESMITH_MAIL_DRY_RUN",
        ),
        Kpi(
            "window",
            window_text(payload.get("window") or {}),
            "info",
            _window_hint(payload.get("window") or {}),
        ),
        Kpi(
            "outbox",
            f"{outbox.get('files', 0)} files · {human_bytes(outbox.get('bytes'))}",
            "muted",
            "data/outbox",
        ),
    ]


def _window_hint(window: dict[str, Any]) -> str:
    """``Europe/Warsaw · cap 20/day`` for the window card's caption."""
    if not isinstance(window, dict) or not window:
        return ""
    parts = [str(window.get("timezone") or "")]
    cap = window.get("daily_cap")
    if cap is not None:
        parts.append(f"cap {cap}/day")
    return " · ".join(part for part in parts if part)


def status_strip(payload: dict[str, Any]) -> str:
    """One dense line: per-status counts, approval channel, data dir."""
    if not payload:
        return ""
    leads = payload.get("leads") or {}
    by_status = dict(leads.get("by_status") or {})
    parts = [
        f"{name} {count}"
        for name, count in sorted(by_status.items(), key=lambda item: (-int(item[1]), item[0]))
    ]
    approval = payload.get("approval_channel") or "interactive"
    return " · ".join(parts) + f"    |    approval: {approval}"


def run_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    runs = payload.get("runs") or {}
    rows: list[dict[str, Any]] = []
    for stage, info in runs.items():
        if not isinstance(info, dict):
            continue
        rows.append(
            {
                "stage": stage,
                "at": str(info.get("at") or "-"),
                "exit_code": info.get("exit_code"),
                "duration_s": info.get("duration_s"),
                "stop_reason": info.get("stop_reason") or "-",
                "error": info.get("error") or "",
            }
        )
    rows.sort(key=lambda row: str(row["at"]), reverse=True)
    return rows


def doctor_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    checks = payload.get("checks") or []
    return [check for check in checks if isinstance(check, dict)]


def doctor_headline(payload: dict[str, Any]) -> Kpi:
    checks = doctor_rows(payload)
    if not checks:
        return Kpi("doctor", "not run", "muted")
    failures = sum(1 for check in checks if check.get("status") == "fail")
    warnings = sum(1 for check in checks if check.get("status") == "warn")
    if failures:
        return Kpi("doctor", f"{failures} failing", "bad", f"{warnings} warning(s)")
    if warnings:
        return Kpi("doctor", f"{warnings} warning(s)", "warn", "no failures")
    return Kpi("doctor", "all clear", "ok")


def cell_text(value: object) -> str:
    """Render one table cell the way the GUI shows it."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value)
    if not text:
        return "-"
    # ``--json`` payloads carry datetimes as ISO strings; the table has room
    # for ``2026-10-25 01:43``, not for microseconds.
    if len(text) >= 16 and text[4] == "-" and text[7] == "-" and text[10] in "T ":
        try:
            return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return text
    return text


def timestamp(value: object) -> str:
    """Compact ``HH:MM:SS`` for run history rows."""
    text = str(value or "")
    if "T" in text:
        return text.split("T", 1)[1][:8]
    return text or "-"
