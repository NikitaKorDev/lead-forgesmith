"""Pure parsing of one raw RFC 5322 message (Phase 4.5).

No I/O, no database, no clock: the whole module is a function of bytes, so every
DSN/reply shape can be tested offline. The parser is deliberately conservative:
it reports what the message *claims* and never decides whether to suppress.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from forgesmith.inbox.models import InboundKind, ParsedInbound

_PARSER = BytesParser(policy=policy.default)
_HEADER_PARSER = BytesHeaderParser(policy=policy.default)

_REPORT_TYPES = {
    "delivery-status": InboundKind.DSN,
    "feedback-report": InboundKind.ARF,
    "disposition-notification": InboundKind.MDN,
}

#: Strong, unambiguous opt-out requests. Matched only against the *unquoted*
#: part of the body, after diacritic folding. Weak words ("stop", "nie") are
#: deliberately absent: a false positive silently burns a lead.
_OPTOUT_PHRASES: tuple[str, ...] = (
    "prosze nie kontaktowac",
    "prosze nie pisac",
    "prosze usunac",
    "prosze wypisac",
    "prosze o usuniecie",
    "nie chce otrzymywac",
    "nie zycze sobie",
    "rezygnuje",
    "wypisz mnie",
    "usun moj adres",
    "unsubscribe",
    "remove me",
    "opt out",
    "stop emailing",
    "do not contact",
)

_QUOTE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*>"),
    re.compile(r"^\s*On\b.+\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*W dniu\b.+\bnapisa", re.IGNORECASE),
    re.compile(r"^\s*Dnia\b.+\bnapisa", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$"),
    re.compile(r"^\s*(From|Od|Sent|Wyslano|To|Do):\s", re.IGNORECASE),
    re.compile(r"^\s*--\s*$"),
)

_DIACRITICS = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O"})
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def canonical_message_id(value: str | None) -> str | None:
    """Normalize a Message-ID for comparison: strip whitespace and brackets.

    ``sends.smtp_message_id`` stores the id *with* angle brackets (see
    ``mailer.mime.make_message_id``), while ``In-Reply-To`` may carry comments
    or folded whitespace. Comparison is case-insensitive because mail servers
    do not preserve the local-part case consistently.
    """
    if not value:
        return None
    text = str(value).strip().strip("<>").strip().lower()
    return text or None


def split_references(value: str | None) -> tuple[str, ...]:
    """Return Message-IDs from ``In-Reply-To``/``References`` in order."""
    if not value:
        return ()
    ids: list[str] = []
    for token in re.findall(r"<[^<>]+>", str(value)):
        canonical = canonical_message_id(token)
        if canonical and canonical not in ids:
            ids.append(canonical)
    if not ids:
        canonical = canonical_message_id(str(value))
        if canonical:
            ids.append(canonical)
    return tuple(ids)


def fold_text(value: str) -> str:
    """Fold Polish diacritics and punctuation for phrase matching."""
    text = value.translate(_DIACRITICS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return _SPACES.sub(" ", text).strip()


def strip_quoted(text: str) -> str:
    """Keep only the text a human typed above the quoted history."""
    kept: list[str] = []
    for line in text.splitlines():
        if any(marker.match(line) for marker in _QUOTE_MARKERS):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def find_optout_phrase(text: str) -> str | None:
    """Return the first strong opt-out phrase in the unquoted body, if any."""
    folded = fold_text(strip_quoted(text))
    if not folded:
        return None
    for phrase in _OPTOUT_PHRASES:
        if phrase in folded:
            return phrase
    return None


def headers_want_body(headers: bytes) -> bool:
    """Whether ``BODY.PEEK[TEXT]`` is worth downloading for this message.

    Only report messages (DSN/ARF/MDN need the machine-readable part) and
    thread replies (the opt-out scan needs the text) qualify. Everything else
    stays header-only, which keeps a 30-day bootstrap cheap.
    """
    message = _HEADER_PARSER.parsebytes(headers)
    content_type = message.get_content_type()
    report_type = message.get_param("report-type")
    if content_type.startswith("multipart/report") or content_type in {
        "message/delivery-status",
        "message/feedback-report",
        "message/disposition-notification",
    }:
        return True
    if isinstance(report_type, str) and report_type:
        return True
    return bool(message.get("In-Reply-To") or message.get("References"))


def _header(message: Message, name: str) -> str | None:
    value = message.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _address(value: str | None) -> str | None:
    if not value:
        return None
    _, address = parseaddr(value)
    address = address.strip().lower()
    return address or None


def _addresses(value: str | None) -> list[str]:
    if not value:
        return []
    return [address.strip().lower() for _, address in getaddresses([value]) if address.strip()]


def _received_at(message: Message) -> datetime | None:
    raw = _header(message, "Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _body_text(message: Message) -> str:
    """Decoded text/plain body (or a crude HTML fallback), never raising."""
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_type() != "text/plain":
            continue
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" in disposition.lower():
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError, KeyError):
            payload = part.get_payload(decode=True)
            content = payload.decode("utf-8", "replace") if payload else ""
        if isinstance(content, str):
            return content
    for part in parts:
        if part.get_content_type() != "text/html":
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError, KeyError):
            continue
        if isinstance(content, str):
            return _TAG.sub(" ", content)
    return ""


def _status_blocks(message: Message) -> list[Message]:
    """Extract every ``message/delivery-status`` block as a header-only message."""
    blocks: list[Message] = []
    for part in message.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        payload = part.get_payload()
        if isinstance(payload, list):
            blocks.extend(item for item in payload if isinstance(item, Message))
            continue
        raw = part.get_payload(decode=True) or b""
        for chunk in re.split(rb"\r?\n\r?\n", raw):
            if not chunk.strip():
                continue
            blocks.append(_HEADER_PARSER.parsebytes(chunk))
    return blocks


def _status_fields(message: Message) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name, value in message.items():
        fields[str(name).strip().lower()] = str(value).strip()
    return fields


def _recipient(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if text.lower().startswith("rfc822;"):
        text = text.split(";", 1)[1].strip()
    return _address(text)


def _smtp_code(diagnostic: str | None) -> int | None:
    if not diagnostic:
        return None
    match = re.search(r"\b([245]\d{2})\b", diagnostic)
    return int(match.group(1)) if match else None


def _embedded_message_id(message: Message) -> str | None:
    """Message-ID of the reported original (DSN) message, if embedded."""
    for part in message.walk():
        if part.get_content_type() not in {"message/rfc822", "text/rfc822-headers"}:
            continue
        payload = part.get_payload()
        inner: Message | None = None
        if isinstance(payload, list) and payload and isinstance(payload[0], Message):
            inner = payload[0]
        else:
            raw = part.get_payload(decode=True) or b""
            if raw.strip():
                inner = _HEADER_PARSER.parsebytes(raw)
        if inner is not None:
            found = _header(inner, "Message-ID")
            if found:
                return found
    return None


def _dedupe_hash(
    *,
    message_id: str | None,
    from_addr: str | None,
    received_at: datetime | None,
    subject: str | None,
    final_recipient: str | None,
    size: int,
) -> str:
    if message_id:
        key = f"id:{canonical_message_id(message_id)}"
    else:
        stamp = received_at.isoformat() if received_at else ""
        key = f"fp:{from_addr or ''}|{stamp}|{subject or ''}|{final_recipient or ''}|{size}"
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()


def parse_message(raw: bytes, *, size: int = 0) -> ParsedInbound:
    """Parse one raw message into a :class:`ParsedInbound`."""
    message = _PARSER.parsebytes(raw)
    size = size or len(raw)

    content_type = message.get_content_type()
    report_type = message.get_param("report-type")
    if not isinstance(report_type, str):
        report_type = None

    from_addr = _address(_header(message, "From"))
    to_addrs = _addresses(_header(message, "To"))
    subject = _header(message, "Subject")
    parsed_at = _received_at(message)
    message_id = _header(message, "Message-ID")

    in_reply_to = split_references(_header(message, "In-Reply-To"))
    references = split_references(_header(message, "References"))
    referenced = tuple(dict.fromkeys(in_reply_to + references))

    auto_submitted = _header(message, "Auto-Submitted")
    auto_reply = _header(message, "X-Autoreply") or _header(message, "X-Autorespond")
    precedence = _header(message, "Precedence")
    list_id = _header(message, "List-Id")

    detail: dict[str, object] = {}
    final_recipient: str | None = None
    reported_id: str | None = None
    enhanced: str | None = None
    smtp_code: int | None = None
    action: str | None = None
    kind: InboundKind

    if report_type in _REPORT_TYPES or content_type in {
        "message/delivery-status",
        "message/feedback-report",
        "message/disposition-notification",
    }:
        kind = _REPORT_TYPES.get(report_type or "")
        if kind is None:
            kind = {
                "message/delivery-status": InboundKind.DSN,
                "message/feedback-report": InboundKind.ARF,
                "message/disposition-notification": InboundKind.MDN,
            }.get(content_type, InboundKind.OTHER)

        if kind is InboundKind.DSN:
            blocks = _status_blocks(message)
            chosen: dict[str, str] | None = None
            for block in blocks:
                fields = _status_fields(block)
                block_action = (fields.get("action") or "").lower()
                if chosen is None:
                    chosen = fields
                if block_action in {"failed", "delayed"}:
                    chosen = fields
                    break
            if chosen is not None:
                final_recipient = _recipient(chosen.get("final-recipient"))
                if final_recipient is None:
                    final_recipient = _recipient(chosen.get("original-recipient"))
                action = (chosen.get("action") or "").lower() or None
                status_text = chosen.get("status") or ""
                enhanced = (
                    status_text if re.match(r"^[245]\.\d{1,3}\.\d{1,3}$", status_text) else None
                )
                diagnostic = chosen.get("diagnostic-code")
                smtp_code = _smtp_code(diagnostic)
                detail["diagnostic"] = diagnostic
                detail["remote_mta"] = chosen.get("remote-mta")
                detail["report_type"] = "delivery-status"
            reported_id = _embedded_message_id(message)
            if reported_id is None:
                reported_id = _header(message, "Original-Message-ID")
            failed = _header(message, "X-Failed-Recipients")
            if failed:
                detail["failed_recipients"] = failed
                if final_recipient is None:
                    final_recipient = _address(failed)
        else:
            detail["report_type"] = report_type or content_type
    elif auto_submitted and auto_submitted.lower() != "no":
        kind = InboundKind.AUTO
        detail["auto_submitted"] = auto_submitted
    elif auto_reply:
        kind = InboundKind.AUTO
        detail["auto_reply"] = auto_reply
    elif precedence and precedence.lower() in {"bulk", "junk", "list"}:
        kind = InboundKind.AUTO
        detail["precedence"] = precedence
    elif list_id:
        kind = InboundKind.AUTO
        detail["list_id"] = list_id
    elif referenced:
        kind = InboundKind.REPLY
    else:
        kind = InboundKind.OTHER

    phrase: str | None = None
    if kind in {InboundKind.REPLY, InboundKind.OTHER}:
        phrase = find_optout_phrase(_body_text(message))
        if phrase is not None:
            kind = InboundKind.OPTOUT
            detail["optout_phrase"] = phrase

    dedupe = _dedupe_hash(
        message_id=message_id,
        from_addr=from_addr,
        received_at=parsed_at,
        subject=subject,
        final_recipient=final_recipient,
        size=size,
    )

    return ParsedInbound(
        kind=kind,
        dedupe_hash=dedupe,
        rfc_message_id=message_id,
        from_addr=from_addr,
        to_addr=to_addrs[0] if to_addrs else None,
        subject=subject,
        received_at=parsed_at,
        final_recipient=final_recipient,
        reported_message_id=reported_id,
        referenced_ids=referenced,
        enhanced_status=enhanced,
        smtp_code=smtp_code,
        action=action,
        optout_phrase=phrase,
        size=size,
        detail=detail,
    )
