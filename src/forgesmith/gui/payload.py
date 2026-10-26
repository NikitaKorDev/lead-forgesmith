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


def rows_of(payload: dict[str, Any], key: str = "rows") -> list[dict[str, Any]]:
    rows = payload.get(key)
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def rows_where_action(payload: dict[str, Any], actions: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep the rows whose shared eligibility state points at one of ``actions``.

    Display-only: the rule itself lives in ``forgesmith.eligibility`` and
    arrives on each row as ``next_action``, so the GUI never re-derives it.
    """
    wanted = set(actions)
    return [row for row in rows_of(payload) if row.get("next_action") in wanted]


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
    suffix = (
        "UTC" if stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0 else ""
    )
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
        return Kpi(
            "doctor",
            f"{failures} failing",
            "bad",
            f"{warnings} warning{_plural(warnings)}",
        )
    if warnings:
        return Kpi(
            "doctor",
            f"{warnings} warning{_plural(warnings)}",
            "warn",
            "no failures",
        )
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


# ------------------------------------------------------------------ the stages
#: The six pipeline stages, in order. Page ids match ``app.NAV_PAGES``.
STAGES: tuple[tuple[str, str, str], ...] = (
    ("collect", "1", "Collect"),
    ("enrich", "2", "Enrich"),
    ("draft", "3", "Draft"),
    ("approve", "4", "Approve"),
    ("send", "5", "Send"),
    ("replies", "6", "Replies"),
)

STAGE_LABELS: dict[str, str] = {page: label for page, _number, label in STAGES}

#: Which ``status --json`` blockers stop a stage from doing its work.
STAGE_BLOCKERS: dict[str, tuple[str, ...]] = {
    "draft": ("ollama", "sender"),
    "send": ("kill_switch", "window"),
}


def _count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _plural(count: int) -> str:
    return "" if count == 1 else "s"


def stage_counts(payload: dict[str, Any]) -> dict[str, int]:
    """Work waiting at each stage, straight from the ``status --json`` funnel.

    Every number is the size of the set that stage's own CLI run would select:
    ``enrichable`` is what ``enrich`` picks up, ``composable`` what ``compose``
    picks up, ``approved`` what ``send`` may deliver, and so on. The strip
    therefore cannot promise work a run would refuse.
    """
    empty = {page: 0 for page, _number, _label in STAGES}
    if not payload:
        return empty
    funnel = payload.get("funnel")
    inbox = payload.get("inbox")
    funnel = funnel if isinstance(funnel, dict) else {}
    inbox = inbox if isinstance(inbox, dict) else {}
    return {
        "collect": _count(funnel.get("collected")),
        "enrich": _count(funnel.get("enrichable")),
        "draft": _count(funnel.get("composable")),
        "approve": _count(funnel.get("drafted")),
        "send": _count(funnel.get("approved")),
        "replies": _count(inbox.get("needs_review")),
    }


@dataclass(frozen=True)
class NextStep:
    """The single thing to do next, and the page that does it."""

    title: str
    detail: str
    page: str
    action: str
    tone: str = "info"


def next_step(payload: dict[str, Any]) -> NextStep:
    """Pick one next step, finishing work in flight before pulling more through.

    The order is deliberate: an unapplied inbound row can be a legal opt-out, a
    draft is one click from being sendable, and only then is it worth drafting
    or enriching more. Without a rule the operator is left staring at eight
    pages of counts.
    """
    if not payload:
        return NextStep(
            "Read the pipeline",
            "status has not loaded yet — press Refresh.",
            "pipeline",
            "Refresh",
            "muted",
        )
    counts = stage_counts(payload)
    funnel = payload.get("funnel")
    funnel = funnel if isinstance(funnel, dict) else {}
    parked = _count(funnel.get("parked"))

    if counts["replies"]:
        return NextStep(
            f"Apply {counts['replies']} inbound row{_plural(counts['replies'])}",
            "Bounces, replies and opt-outs wait in the review ledger. "
            "Opt-outs suppress only the sender's address.",
            "replies",
            "Open Replies",
            "warn",
        )
    if counts["approve"]:
        return NextStep(
            f"Review {counts['approve']} draft{_plural(counts['approve'])}",
            "A draft already exists. Read it, then approve it to queue it for sending.",
            "approve",
            "Open Approve",
            "warn",
        )
    if counts["send"]:
        return NextStep(
            f"Send {counts['send']} approved message{_plural(counts['send'])}",
            "Approved mail waits in the queue. Dry run first; live send also needs "
            "the kill switch off and the window open.",
            "send",
            "Open Send",
            "info",
        )
    if counts["draft"]:
        return NextStep(
            f"Draft {counts['draft']} email{_plural(counts['draft'])}",
            "These leads are enriched and have a role address, with no draft yet.",
            "draft",
            "Open Draft",
            "info",
        )
    if counts["enrich"]:
        detail = "Resolve storefronts and extract role-first addresses."
        if parked:
            detail += f" {parked} parked lead{_plural(parked)} can be retried with Force."
        return NextStep(
            f"Enrich {counts['enrich']} lead{_plural(counts['enrich'])}",
            detail,
            "enrich",
            "Open Enrich",
            "info",
        )
    if counts["collect"] == 0:
        return NextStep(
            "Collect the first leads",
            "Run a source stage to fill the database, then enrich them.",
            "collect",
            "Open Collect",
            "info",
        )
    return NextStep(
        "Nothing is waiting",
        "No lead needs attention at any stage. Collect more, or check Tools.",
        "pipeline",
        "Refresh",
        "ok",
    )


@dataclass(frozen=True)
class Blocker:
    """One precondition the CLI reports as blocked."""

    stage: str
    key: str
    text: str
    tone: str = "warn"


def _stage_for_blocker(key: str) -> str:
    for stage, keys in STAGE_BLOCKERS.items():
        if key in keys:
            return stage
    return "pipeline"


def blockers_for(payload: dict[str, Any], stage: str | None = None) -> list[Blocker]:
    """Blocked preconditions, optionally only those one stage owns.

    Reported only when ``status --json`` says ``blocked``: the GUI never
    guesses, and the wording is the CLI's own.
    """
    blockers = payload.get("blockers")
    if not isinstance(blockers, dict):
        return []
    keys = STAGE_BLOCKERS.get(stage, ()) if stage else tuple(blockers)
    found: list[Blocker] = []
    for key in keys:
        info = blockers.get(key)
        if not isinstance(info, dict) or not info.get("blocked"):
            continue
        found.append(
            Blocker(
                stage=_stage_for_blocker(key),
                key=key,
                text=str(info.get("detail") or key),
            )
        )
    return found


def funnel_line(payload: dict[str, Any]) -> str:
    """``400 leads · 43 with a domain · 357 domainless · 3 parked``."""
    funnel = payload.get("funnel")
    if not isinstance(funnel, dict) or not funnel:
        return ""
    parts = [
        f"{_count(funnel.get('collected'))} leads",
        f"{_count(funnel.get('with_domain'))} with a domain",
    ]
    for key, label in (("domainless", "domainless"), ("parked", "parked"), ("done", "finished")):
        count = _count(funnel.get(key))
        if count:
            parts.append(f"{count} {label}")
    return " · ".join(parts)
