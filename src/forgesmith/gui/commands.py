"""Declarative command catalogue for the desktop front end.

Deliberately Qt-free: ``tests/test_gui_commands.py`` imports this module without
PyQt5 installed. The GUI builds every form, table and confirmation prompt from
these specs, so adding a CLI flag is a one-line change here instead of a new
widget.

The GUI never re-implements pipeline logic. It renders argv, starts the real
``python -m forgesmith`` process, and shows its output, so gates, locks, the run
ledger and the kill switch keep working exactly as they do on the command line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LEAD_STATUSES = (
    "new",
    "enriched",
    "composed",
    "queued",
    "sent",
    "replied",
    "bounced",
    "suppressed",
    "skipped",
)
SEND_STATUSES = ("pending", "sent", "failed", "bounced", "uncertain", "aborted")
INBOUND_STATUSES = ("new", "applied", "needs_review", "ignored", "failed")
SOURCES = ("shopify", "olx")
ENGINES = ("auto", "http", "browser")


class MissingFields(ValueError):
    """Raised when a required form field is empty; the GUI shows it inline."""

    def __init__(self, labels: list[str]) -> None:
        self.labels = labels
        super().__init__("required: " + ", ".join(labels))


@dataclass(frozen=True)
class Field:
    """One form control and the argv flag it feeds."""

    key: str
    label: str
    kind: str = "text"  # text | int | float | path | choice | bool | multi
    default: Any = None
    help: str = ""
    choices: tuple[str, ...] = ()
    required: bool = False
    minimum: int | None = None
    maximum: int | None = None
    placeholder: str = ""
    advanced: bool = False
    flag_name: str | None = None
    #: Emitted when the box is cleared (e.g. ``backup --no-outbox``).
    negated_flag: str | None = None

    @property
    def flag(self) -> str:
        return self.flag_name or "--" + self.key.replace("_", "-")


@dataclass(frozen=True)
class Command:
    """One CLI invocation the GUI can run."""

    id: str
    label: str
    argv: tuple[str, ...]
    summary: str = ""
    fields: tuple[Field, ...] = ()
    #: Appends ``--yes``: the GUI's modal dialog is the human confirmation, and
    #: a subprocess never has a TTY, so the CLI would otherwise refuse.
    auto_yes: bool = False
    #: Confirmation prompt. ``None`` means "safe, run immediately".
    danger: str | None = None
    #: Only confirm when one of these values is truthy (e.g. ``send --live``).
    danger_if: tuple[str, ...] = ()
    #: At least one of these fields must be filled (e.g. ``mark`` needs a lead
    #: or a send). Without it the CLI would refuse the run after a modal.
    requires_one_of: tuple[str, ...] = ()
    requires_one_of_label: str = ""
    #: Free-text hint shown above the form (used for the live-send warning).
    notice: str | None = None
    long_running: bool = False


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    width: int = 120
    align: str = "left"


# --------------------------------------------------------------------- tables
TABLES: dict[str, tuple[Column, ...]] = {
    "leads": (
        Column("id", "id", 50, "right"),
        Column("source", "source", 70),
        Column("name", "name", 240),
        Column("domain", "domain", 170),
        Column("city", "city", 100),
        Column("category", "category", 110),
        Column("status", "status", 90),
        Column("platform", "platform", 80),
    ),
    "contacts": (
        Column("lead_id", "lead", 50, "right"),
        Column("lead_name", "company", 200),
        Column("email", "email", 230),
        Column("kind", "kind", 70),
        Column("confidence", "confidence", 80, "right"),
        Column("source_url", "source", 260),
    ),
    "drafts": (
        Column("message_id", "msg", 55, "right"),
        Column("lead_id", "lead", 50, "right"),
        Column("company", "company/domain", 170),
        Column("contact", "contact", 210),
        Column("subject", "subject", 300),
        Column("lead_status", "lead status", 90),
        Column("created_at", "created", 140),
    ),
    "sends": (
        Column("id", "send", 50, "right"),
        Column("lead_id", "lead", 50, "right"),
        Column("message_id", "msg", 50, "right"),
        Column("to", "to", 210),
        Column("mode", "mode", 70),
        Column("status", "status", 90),
        Column("error", "error", 240),
        Column("created_at", "created", 130),
        Column("sent_at", "sent", 130),
    ),
    "outbox": (
        Column("file", "file", 200),
        Column("size", "size", 80, "right"),
        Column("modified", "modified", 140),
        Column("path", "path", 340),
    ),
    "inbound": (
        Column("id", "id", 45, "right"),
        Column("kind", "kind", 70),
        Column("status", "status", 100),
        Column("from", "from", 200),
        Column("subject", "subject", 280),
        Column("lead_id", "lead", 50, "right"),
        Column("send_id", "send", 50, "right"),
        Column("seen_at", "seen", 140),
    ),
    "doctor": (
        Column("check", "check", 150),
        Column("status", "status", 80),
        Column("detail", "detail", 600),
    ),
    "runs": (
        Column("stage", "stage", 110),
        Column("at", "last run", 200),
        Column("exit_code", "exit", 55, "right"),
        Column("duration_s", "secs", 70, "right"),
        Column("stop_reason", "stop", 110),
        Column("error", "error", 300),
    ),
}


# ------------------------------------------------------------------- commands
COMMANDS: tuple[Command, ...] = (
    Command(
        id="source_shopify",
        label="Probe Shopify seeds",
        argv=("source", "shopify"),
        summary="Fetch /products.json for every seed domain and store catalogue metadata.",
        long_running=True,
        fields=(
            Field(
                "seeds",
                "Seed files",
                kind="multi",
                placeholder="default: data/shopify_seeds.txt",
                help="Comma-separated paths. Empty uses data/shopify_seeds.txt.",
            ),
            Field("limit", "Max leads to store", kind="int", minimum=1, placeholder="no limit"),
        ),
    ),
    Command(
        id="source_olx",
        label="Crawl OLX listings",
        argv=("source", "olx"),
        summary="Drive headless Chromium through OLX listing pages (business ads only).",
        long_running=True,
        notice="OLX sits behind a CloudFront WAF: the browser engine is the working path.",
        fields=(
            Field(
                "query",
                "Queries",
                kind="multi",
                default=("meble",),
                placeholder="meble, sofy",
                help="Comma-separated search phrases.",
            ),
            Field(
                "city",
                "Cities",
                kind="multi",
                placeholder="warszawa, krakow",
                help="Comma-separated OLX city slugs.",
            ),
            Field(
                "category",
                "Categories",
                kind="multi",
                placeholder="nieruchomosci/mieszkania/wynajem",
            ),
            Field("pages", "Pages per query/city", kind="int", default=2, minimum=1, maximum=20),
            Field("limit", "Max leads to store", kind="int", minimum=1, placeholder="no limit"),
            Field(
                "engine",
                "Engine",
                kind="choice",
                choices=("browser", "http"),
                default="browser",
            ),
        ),
    ),
    Command(
        id="enrich",
        label="Enrich leads",
        argv=("enrich",),
        summary="Resolve storefronts, extract role-first emails, NIP and page context.",
        long_running=True,
        fields=(
            Field("limit", "Max leads", kind="int", minimum=1, placeholder="all pending"),
            Field("source", "Source", kind="choice", choices=SOURCES, placeholder="any"),
            Field("lead_id", "Lead id", kind="int", minimum=1, placeholder="single lead"),
            Field(
                "engine",
                "Engine",
                kind="choice",
                choices=ENGINES,
                default="auto",
                help="auto = HTTP first, Chromium only after a block.",
            ),
            Field(
                "resolve_only",
                "Resolve domains only",
                kind="bool",
                help="Write candidate domains, do not crawl.",
            ),
            Field(
                "deep_resolve",
                "Deep resolve",
                kind="bool",
                help="Try OLX offer pages when no shop subdomain exists.",
            ),
            Field("force", "Force re-enrich", kind="bool", advanced=True),
        ),
    ),
    Command(
        id="compose",
        label="Generate drafts",
        argv=("compose",),
        summary="Turn enriched leads into grounded Polish drafts with local Ollama.",
        long_running=True,
        fields=(
            Field("limit", "Max leads", kind="int", minimum=1, placeholder="all eligible"),
            Field("source", "Source", kind="choice", choices=SOURCES, placeholder="any"),
            Field("lead_id", "Lead id", kind="int", minimum=1, placeholder="single lead"),
            Field("regenerate", "Regenerate", kind="bool", help="Draft again even if one exists."),
            Field(
                "retry_failed",
                "Retry failed",
                kind="bool",
                help="Retry leads that reached the failed-attempt cap.",
            ),
        ),
    ),
    Command(
        id="approve",
        label="Approve",
        argv=("approve",),
        auto_yes=True,
        summary="Move composed -> queued so the mailer may send it.",
        danger=(
            "Approve the latest draft?\n\n"
            "Approval moves the lead to `queued`, which makes it eligible for the "
            "next send run. Review the body first: the mailer re-checks status, "
            "suppression and footer version, but it cannot judge the wording."
        ),
        fields=(
            Field("lead_id", "Lead id", kind="int", minimum=1, placeholder="single lead"),
            Field("all_composed", "All composed leads", kind="bool"),
            Field("message_id", "Assert message id", kind="int", minimum=1),
            Field("limit", "Max leads (bulk)", kind="int", minimum=1),
        ),
        requires_one_of=("lead_id", "all_composed"),
        requires_one_of_label="Lead id or 'All composed leads'",
    ),
    Command(
        id="unapprove",
        label="Unapprove",
        argv=("unapprove",),
        summary="Retract approval: queued -> composed (only while no live send exists).",
        danger="Retract this approval? The lead returns to `composed` and will not be sent.",
        fields=(Field("lead_id", "Lead id", kind="int", minimum=1, required=True),),
    ),
    Command(
        id="send",
        label="Send",
        argv=("send",),
        auto_yes=True,
        summary="Dry run renders .eml evidence; --live delivers over SMTP.",
        long_running=True,
        danger_if=("live",),
        danger=(
            "LIVE SEND\n\n"
            "This opens SMTP and delivers real email to real businesses.\n\n"
            "It only works when FORGESMITH_MAIL_DRY_RUN=false (the kill switch) "
            "and the sender/SMTP settings are complete. Caps, the sending window "
            "and suppressions are still enforced.\n\n"
            "Continue with the live run?"
        ),
        notice=(
            "Dry run writes .eml files to data/outbox and sends nothing. "
            "Tick 'Live SMTP' only when you mean it."
        ),
        fields=(
            Field("limit", "Max messages", kind="int", minimum=1, placeholder="policy default"),
            Field("lead_id", "Lead id", kind="int", minimum=1),
            Field("source", "Source", kind="choice", choices=SOURCES, placeholder="any"),
            Field("live", "Live SMTP", kind="bool", help="Off = dry run."),
            Field("no_delay", "Skip jitter delay", kind="bool"),
            Field("force_window", "Ignore window", kind="bool", help="Never ignores the cap."),
            Field(
                "max_runtime",
                "Runtime budget (s)",
                kind="float",
                minimum=0,
                placeholder="FORGESMITH_SEND_MAX_RUNTIME",
            ),
        ),
    ),
    Command(
        id="inbox_sync",
        label="Sync inbox",
        argv=("inbox", "sync"),
        summary="Read new mail over read-only IMAP; apply hard bounces and matched replies.",
        long_running=True,
        fields=(
            Field("dry_run", "Dry run (parse only)", kind="bool"),
            Field("limit", "Max messages", kind="int", minimum=1),
        ),
    ),
    Command(
        id="inbox_show",
        label="Explain inbound",
        argv=("inbox", "show"),
        summary="Show one inbound ledger row, its evidence and the action it proposes.",
        fields=(Field("id", "Inbound id", kind="int", minimum=1, required=True),),
    ),
    Command(
        id="inbox_apply",
        label="Apply inbound",
        argv=("inbox", "apply"),
        auto_yes=True,
        summary="Apply a stored outcome. Opt-outs suppress only the sender's address.",
        danger=(
            "Apply this stored outcome?\n\n"
            "Opt-out rows suppress the sender's email address (never the domain). "
            "Review rows are never applied automatically."
        ),
        fields=(
            Field("id", "Inbound id", kind="int", minimum=1),
            Field("all", "All new rows", kind="bool"),
            Field("limit", "Max rows (all)", kind="int", minimum=1, default=200),
        ),
        requires_one_of=("id", "all"),
        requires_one_of_label="Inbound id or 'All new rows'",
    ),
    Command(
        id="backup",
        label="Backup",
        argv=("backup",),
        summary="Consistent SQLite VACUUM INTO snapshot, plus an outbox archive.",
        fields=(
            Field("out", "Output directory", kind="path", placeholder="data/backups"),
            Field("keep", "Keep newest N", kind="int", minimum=0, default=0),
            Field(
                "with_outbox",
                "Archive .eml evidence",
                kind="bool",
                default=True,
                flag_name="--outbox",
                negated_flag="--no-outbox",
            ),
        ),
    ),
    Command(
        id="doctor",
        label="Doctor",
        argv=("doctor",),
        summary="Pre-flight checks. Exit 1 = fail, 3 = warning.",
        fields=(
            Field(
                "smtp_check",
                "Also log in to SMTP",
                kind="bool",
                help="Connects, STARTTLS and authenticates.",
            ),
        ),
    ),
    Command(
        id="db_init",
        label="Init database",
        argv=("db", "init"),
        summary="Create tables and apply additive schema pieces (idempotent).",
    ),
    Command(
        id="stats",
        label="Stats",
        argv=("stats",),
        summary="Lead counts by source and status.",
    ),
    Command(
        id="version",
        label="Version",
        argv=("version",),
        summary="Print the installed version.",
    ),
    Command(
        id="mark",
        label="Mark outcome",
        argv=("mark",),
        auto_yes=True,
        summary="Record a reply/bounce manually, or resolve an uncertain send.",
        danger=(
            "Record this outcome in the ledger?\n\n"
            "Lead outcomes are monotonic and audited; resolving a send decides "
            "whether an uncertain attempt counts as delivered or failed."
        ),
        fields=(
            Field("lead_id", "Lead id", kind="int", minimum=1),
            Field("send_id", "Send id", kind="int", minimum=1),
            Field(
                "status",
                "Status",
                kind="choice",
                choices=("replied", "bounced", "sent", "suppressed", "failed"),
                required=True,
            ),
            Field("reason", "Reason", kind="text", placeholder="why (audited)"),
        ),
        requires_one_of=("lead_id", "send_id"),
        requires_one_of_label="Lead id or Send id",
    ),
    Command(
        id="suppress",
        label="Suppress",
        argv=("suppress",),
        summary="Record an email or domain opt-out; checked before every send.",
        danger=(
            "Add a suppression?\n\n"
            "No future send will target this address/domain until you unsuppress it."
        ),
        fields=(
            Field("email", "Email", kind="text", placeholder="biuro@example.pl"),
            Field("domain", "Domain", kind="text", placeholder="example.pl"),
            Field("reason", "Reason", kind="text", placeholder="why (audited)"),
        ),
        requires_one_of=("email", "domain"),
        requires_one_of_label="Email or Domain",
    ),
    Command(
        id="unsuppress",
        label="Unsuppress",
        argv=("unsuppress",),
        summary="Reverse a suppression with an audit event.",
        danger="Remove this suppression? The address becomes eligible for future sends.",
        fields=(
            Field("email", "Email", kind="text", placeholder="biuro@example.pl"),
            Field("domain", "Domain", kind="text", placeholder="example.pl"),
            Field("reason", "Reason", kind="text", placeholder="why (audited)"),
        ),
        requires_one_of=("email", "domain"),
        requires_one_of_label="Email or Domain",
    ),
)

COMMANDS_BY_ID: dict[str, Command] = {command.id: command for command in COMMANDS}


def command(name: str) -> Command:
    return COMMANDS_BY_ID[name]


# ----------------------------------------------------------------- argv build
def build_argv(spec: Command, values: dict[str, Any]) -> list[str]:
    """Turn form values into argv, skipping blanks and raising on required ones."""
    argv = list(spec.argv)
    missing: list[str] = []
    emitted: set[str] = set()

    for field in spec.fields:
        value = values.get(field.key, field.default)

        if field.kind == "bool":
            if value:
                argv.append(field.flag)
                emitted.add(field.key)
            elif field.negated_flag:
                argv.append(field.negated_flag)
            continue

        if field.kind == "multi":
            if value is None:
                if field.required:
                    missing.append(field.label)
                continue
            items = value if isinstance(value, (list, tuple)) else [value]
            filled = False
            for item in items:
                text = str(item).strip()
                if text:
                    argv.extend([field.flag, text])
                    filled = True
            if filled:
                emitted.add(field.key)
            elif field.required:
                missing.append(field.label)
            continue

        if value is None:
            if field.required:
                missing.append(field.label)
            continue

        if isinstance(value, str):
            value = value.strip()
            if not value:
                if field.required:
                    missing.append(field.label)
                continue

        if field.kind in {"int", "float"}:
            try:
                number = int(value) if field.kind == "int" else float(value)
            except (TypeError, ValueError):
                missing.append(f"{field.label} (not a number)")
                continue
            if field.minimum is not None and number < field.minimum:
                missing.append(f"{field.label} (min {field.minimum})")
                continue
            if field.maximum is not None and number > field.maximum:
                missing.append(f"{field.label} (max {field.maximum})")
                continue
            value = number

        argv.extend([field.flag, str(value)])
        emitted.add(field.key)

    if spec.requires_one_of and not (emitted & set(spec.requires_one_of)):
        missing.append(spec.requires_one_of_label or "one of " + ", ".join(spec.requires_one_of))
    if missing:
        raise MissingFields(missing)
    if spec.auto_yes:
        argv.append("--yes")
    return argv


class _SafeValues(dict):
    def __missing__(self, key: str) -> str:
        return ""


def confirmation(spec: Command, values: dict[str, Any]) -> str | None:
    """Return the confirmation text, or ``None`` when the command is safe."""
    if not spec.danger:
        return None
    if spec.danger_if and not any(values.get(key) for key in spec.danger_if):
        return None
    return spec.danger.format_map(_SafeValues(values))


def is_dangerous(spec: Command, values: dict[str, Any]) -> bool:
    return confirmation(spec, values) is not None


# ---------------------------------------------------------------- json output
JSON_COMMANDS: dict[str, tuple[str, ...]] = {
    "leads": ("leads", "--json"),
    "contacts": ("contacts", "--json"),
    "drafts": ("drafts", "--json"),
    "sends": ("sends", "--json"),
    "outbox": ("outbox", "--json"),
    "inbound": ("inbox", "list", "--json"),
}


_TONE_OK = frozenset({"ok", "sent", "replied", "applied", "enriched", "composed"})
_TONE_WARN = frozenset(
    {"warn", "warning", "pending", "needs_review", "uncertain", "bounced", "queued"}
)
_TONE_BAD = frozenset({"fail", "error", "aborted", "failed"})
_TONE_INFO = frozenset({"new", "info", "suppressed"})
_TONE_MUTED = frozenset({"skipped", "ignored", ""})


def status_tone(value: object) -> str:
    """Map a status string to a palette key (ok/warn/bad/info/muted)."""
    text = str(value or "").strip().lower()
    if text in _TONE_OK:
        return "ok"
    if text in _TONE_WARN:
        return "warn"
    if text in _TONE_BAD:
        return "bad"
    if text in _TONE_INFO:
        return "info"
    return "muted"


def exit_tone(code: int) -> tuple[str, str]:
    """Return ``(label, tone)`` for a CLI exit code."""
    if code == 0:
        return "success", "ok"
    if code == 2:
        return "refused", "warn"
    if code == 3:
        return "warnings", "warn"
    if code == 75:
        return "busy (skipped)", "warn"
    return "failed", "bad"
