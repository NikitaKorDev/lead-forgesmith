"""Value objects for the read-only IMAP ingest (Phase 4.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from forgesmith.mailer.models import MailerError


class InboxConfigError(MailerError):
    """IMAP settings are missing or unusable."""


class InboxError(MailerError):
    """The mailbox could not be read (protocol or server failure)."""


class InboundKind(StrEnum):
    DSN = "dsn"
    REPLY = "reply"
    OPTOUT = "optout"
    ARF = "arf"
    MDN = "mdn"
    AUTO = "auto"
    OTHER = "other"


class InboundStatus(StrEnum):
    NEW = "new"
    APPLIED = "applied"
    NEEDS_REVIEW = "needs_review"
    IGNORED = "ignored"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RawMessage:
    """One message as fetched from a mailbox, before parsing."""

    mailbox: str
    uid: int
    uid_validity: int
    headers: bytes
    body: bytes | None
    size: int

    @property
    def raw(self) -> bytes:
        if self.body is None:
            return self.headers
        return self.headers + b"\r\n" + self.body


@dataclass(frozen=True, slots=True)
class ParsedInbound:
    """Pure parse result. ``detail`` never contains the message body."""

    kind: InboundKind
    dedupe_hash: str
    rfc_message_id: str | None = None
    from_addr: str | None = None
    to_addr: str | None = None
    subject: str | None = None
    received_at: datetime | None = None
    final_recipient: str | None = None
    reported_message_id: str | None = None
    referenced_ids: tuple[str, ...] = ()
    enhanced_status: str | None = None
    smtp_code: int | None = None
    action: str | None = None
    optout_phrase: str | None = None
    size: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def is_hard_bounce(self) -> bool:
        return (
            self.kind is InboundKind.DSN
            and (self.action or "").lower() == "failed"
            and bool(self.enhanced_status)
            and self.enhanced_status.startswith("5.")
        )

    @property
    def is_soft_bounce(self) -> bool:
        return (
            self.kind is InboundKind.DSN
            and (self.action or "").lower() in {"failed", "delayed"}
            and (
                (self.enhanced_status or "").startswith("4.")
                or (self.smtp_code is not None and 400 <= self.smtp_code < 500)
            )
        )


@dataclass(slots=True)
class SyncStats:
    scanned: int = 0
    ingested: int = 0
    duplicates: int = 0
    applied: int = 0
    replies: int = 0
    bounces: int = 0
    needs_review: int = 0
    ignored: int = 0
    failed: int = 0
    dry_run: int = 0
    uidvalidity_reset: int = 0
    stop_reason: str | None = None
    duration_s: float = 0.0


@dataclass(slots=True)
class ApplyStats:
    applied: int = 0
    skipped: int = 0
    refused: int = 0
