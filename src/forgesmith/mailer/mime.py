"""MIME rendering and ``.eml`` evidence files.

``messages.body`` already contains the Phase 3 footer and is sent verbatim; the
footer must never be appended here.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from pathlib import Path

from forgesmith.mailer.models import SendConfigError


def _assert_single_line(value: str, field: str) -> str:
    if not value or not value.strip():
        raise SendConfigError(f"{field} is empty")
    if any(ch in value for ch in "\r\n") or any(ord(ch) < 32 for ch in value):
        raise SendConfigError(f"{field} contains a control character")
    return value


def validate_address(address: str) -> str:
    address = _assert_single_line(address.strip(), "email address")
    if not address.isascii():
        raise SendConfigError("email address must be ASCII")
    local, separator, domain = address.partition("@")
    if not separator or not local or not domain or "@" in domain or " " in address:
        raise SendConfigError(f"invalid email address: {address!r}")
    return address


def make_message_id(sender_address: str, now: datetime | None = None) -> str:
    """Generate the SMTP ``Message-ID`` stored on the send ledger."""
    domain = sender_address.rsplit("@", 1)[-1] or "localhost"
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    return f"<{stamp}.{uuid.uuid4().hex}@{domain}>"


def build_email(
    *,
    subject: str,
    body: str,
    sender_name: str,
    sender_address: str,
    recipient: str,
    message_id: str,
    date: datetime | None = None,
) -> EmailMessage:
    subject = _assert_single_line(subject.strip(), "subject")
    sender_address = validate_address(sender_address)
    recipient = validate_address(recipient)
    _assert_single_line(message_id, "message id")
    sender_name = _assert_single_line(sender_name, "sender name")

    message = EmailMessage(policy=SMTP_POLICY)
    message["From"] = f"{sender_name} <{sender_address}>"
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = date or datetime.now(UTC)
    message["Message-ID"] = message_id
    # Polish text is 8-bit; force a 7-bit-safe transfer encoding.
    message.set_content(body, subtype="plain", charset="utf-8", cte="quoted-printable")
    return message


def render_bytes(message: EmailMessage) -> bytes:
    return message.as_bytes(policy=SMTP_POLICY)


def resolve_sender(sender: object) -> tuple[str, str]:
    """Return ``(name, address)`` from a ``SenderProfile``-like object."""
    name = str(getattr(sender, "name", "") or "").strip()
    address = str(getattr(sender, "email", "") or "").strip()
    if not name or not address:
        raise SendConfigError("MAIL_FROM_NAME and MAIL_FROM_ADDRESS are required")
    return _assert_single_line(name, "sender name"), validate_address(address)


def dry_run_path(outbox: Path, lead_id: int, message_id: int) -> Path:
    return outbox / f"lead{lead_id}-msg{message_id}.eml"


def live_path(outbox: Path, lead_id: int, message_id: int, send_id: int) -> Path:
    return outbox / f"lead{lead_id}-msg{message_id}-s{send_id}.eml"


def write_eml(path: Path, raw: bytes, *, overwrite: bool = True) -> Path:
    """Write atomically (``tmp`` + ``os.replace``) so a crash cannot leave half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return path
