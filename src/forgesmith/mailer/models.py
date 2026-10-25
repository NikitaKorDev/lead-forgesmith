"""Value objects shared by the Phase 4 mailer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SendMode(StrEnum):
    DRY_RUN = "dry_run"
    LIVE = "live"


class SendStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    BOUNCED = "bounced"
    UNCERTAIN = "uncertain"
    ABORTED = "aborted"


#: Statuses proving a message may have reached the recipient. Used by every
#: uniqueness and double-send guard.
LIVE_STATUSES: tuple[str, ...] = (
    SendStatus.PENDING,
    SendStatus.SENT,
    SendStatus.UNCERTAIN,
)

#: Statuses that consume one of ``mail_max_attempts``. ``aborted`` is an
#: infrastructure state before MAIL FROM and deliberately does not count.
ATTEMPT_STATUSES: tuple[str, ...] = (
    SendStatus.SENT,
    SendStatus.FAILED,
    SendStatus.BOUNCED,
    SendStatus.UNCERTAIN,
)

#: ``queued`` leads that may still receive mail (``replied``/``suppressed``
#: are terminal and never selected).
TERMINAL_LEAD_STATUSES: frozenset[str] = frozenset({"sent", "replied", "bounced", "suppressed"})


class RecheckReason(StrEnum):
    NOT_QUEUED = "not_queued"
    STALE_APPROVAL = "stale_approval"
    NOT_APPROVED = "not_approved"
    CONTACT_MISSING = "contact_missing"
    CONTACT_CHANGED = "contact_changed"
    EMAIL_INVALID = "email_invalid"
    SUPPRESSED = "suppressed"
    STALE_FOOTER = "stale_footer"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    RETRY_NOT_BEFORE = "retry_not_before"
    EXISTING_ATTEMPT = "existing_attempt"
    DOMAIN_ALREADY_CONTACTED = "domain_already_contacted"
    RECIPIENT_ALREADY_CONTACTED = "recipient_already_contacted"
    OUTSIDE_WINDOW = "outside_window"
    DAILY_CAP = "daily_cap"
    NOT_ALLOWLISTED = "not_allowlisted"
    CONFIG_INVALID = "config_invalid"


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """Frozen selection snapshot; never an ORM instance.

    ``expire_on_commit=False`` keeps detached ORM objects looking alive after
    their session closes, so selection deliberately copies every field it
    needs into this value object.
    """

    lead_id: int
    source: str
    lead_domain: str | None
    message_id: int
    contact_id: int | None
    to_email: str
    subject: str
    body: str
    prompt_version: str | None
    footer_version: str | None
    attempts: int = 0
    last_failed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecheckResult:
    """Outcome of the authoritative pre-send re-check."""

    reason: str | None = None
    stop: bool = False
    defer: bool = False
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.reason is None


@dataclass(frozen=True, slots=True)
class Reservation:
    send_id: int
    message_id: int
    to_email: str
    smtp_message_id: str


@dataclass(slots=True)
class SendStats:
    """Counters for one send run."""

    scanned: int = 0
    dry_run: int = 0
    sent: int = 0
    failed: int = 0
    bounced: int = 0
    uncertain: int = 0
    aborted: int = 0
    skipped: int = 0
    deferred: int = 0
    uncertain_recovered: int = 0
    uncertain_outstanding: int = 0
    stop_reason: str | None = None
    duration_s: float = 0.0


@dataclass(slots=True)
class ApproveStats:
    approved: int = 0
    already: int = 0
    skipped: int = 0
    refused: int = 0


class MailerError(Exception):
    """Base class for expected Phase 4 failures."""


class SendConfigError(MailerError):
    """Sender identity, timezone, window, or SMTP configuration is unusable."""


class SendRefused(MailerError):
    """The operator did not confirm a live run; the CLI maps this to exit 2."""


class LockHeldError(MailerError):
    def __init__(self, path: str, pid: int | None = None) -> None:
        self.path = path
        self.pid = pid
        holder = f" (pid {pid})" if pid else ""
        super().__init__(f"mailer lock is held{holder}: {path}")


class ApprovalRefused(MailerError):
    def __init__(self, reason: str, detail: dict | None = None) -> None:
        self.reason = reason
        self.detail = detail or {}
        super().__init__(reason)


class TransportError(MailerError):
    """Connect/TLS/login failed before any reservation was written."""
