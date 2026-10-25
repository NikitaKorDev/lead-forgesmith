"""SMTP transport with an explicit, stage-tagged delivery contract.

``smtplib.sendmail`` collapses every failure into one exception, so it cannot
tell "the relay rejected the recipient" from "the relay may already have
accepted the message". Phase 4 therefore drives ``mail``/``rcpt``/``data``
manually and maps each stage to a send-ledger status.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from forgesmith.mailer.models import SendStatus, TransportError

_ENHANCED_STATUS = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")
_MAX_RESPONSE = 500


@dataclass(frozen=True, slots=True)
class SendOutcome:
    status: str
    stage: str
    code: int | None = None
    response: str | None = None
    enhanced: str | None = None
    detail: str | None = None

    @property
    def delivered(self) -> bool:
        return self.status == SendStatus.SENT

    @property
    def may_have_delivered(self) -> bool:
        return self.status == SendStatus.UNCERTAIN


class MailTransport(Protocol):
    async def open(self) -> None: ...

    async def deliver(
        self,
        *,
        sender: str,
        recipient: str,
        raw: bytes,
        message_id: str,
    ) -> SendOutcome: ...

    async def close(self) -> None: ...


def _trim(value: object) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return text.strip()[:_MAX_RESPONSE]


def enhanced_status(response: str | None) -> str | None:
    if not response:
        return None
    match = _ENHANCED_STATUS.search(response)
    return match.group(1) if match else None


def is_hard_mailbox_rejection(code: int, response: str | None) -> bool:
    enhanced = enhanced_status(response)
    return code == 550 and enhanced is None


def classify_rcpt(code: int, response: str | None) -> str:
    enhanced = enhanced_status(response)
    if enhanced and enhanced.startswith("5.1."):
        return SendStatus.BOUNCED
    return SendStatus.FAILED


def classify_data(code: int, response: str | None) -> str:
    if code == 250:
        return SendStatus.SENT
    return SendStatus.FAILED


class SmtpTransport:
    """Real SMTP transport; the client factory is the test seam."""

    def __init__(
        self,
        cfg: object,
        *,
        client_factory: Callable[[], smtplib.SMTP] | None = None,
    ) -> None:
        self.host = str(getattr(cfg, "smtp_host", ""))
        self.port = int(getattr(cfg, "smtp_port", 587))
        self.user = str(getattr(cfg, "smtp_user", "") or "")
        self.password = str(getattr(cfg, "smtp_password", "") or "")
        self.use_ssl = bool(getattr(cfg, "smtp_ssl", False))
        self.starttls = bool(getattr(cfg, "smtp_starttls", True))
        self.timeout = float(getattr(cfg, "smtp_timeout", 30.0))
        self._client_factory = client_factory or self._default_factory
        self._client: smtplib.SMTP | None = None

    def _default_factory(self) -> smtplib.SMTP:
        if not self.host:
            raise TransportError("SMTP_HOST is empty")
        if self.use_ssl:
            return smtplib.SMTP_SSL(
                self.host,
                self.port,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)

    async def open(self) -> None:
        import asyncio

        try:
            self._client = await asyncio.to_thread(self._open_sync)
        except TransportError:
            raise
        except Exception as exc:  # smtplib/SSL/socket failures are all fatal here
            raise TransportError(f"smtp open failed: {type(exc).__name__}: {exc}") from exc

    def _open_sync(self) -> smtplib.SMTP:
        client = self._client_factory()
        client.ehlo()
        if self.starttls and not self.use_ssl:
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if self.user:
            client.login(self.user, self.password)
        return client

    async def deliver(
        self,
        *,
        sender: str,
        recipient: str,
        raw: bytes,
        message_id: str,
    ) -> SendOutcome:
        import asyncio

        if self._client is None:
            raise TransportError("smtp transport is not open")
        return await asyncio.to_thread(self._deliver_sync, sender, recipient, raw)

    def _deliver_sync(self, sender: str, recipient: str, raw: bytes) -> SendOutcome:
        client = self._client
        assert client is not None

        try:
            code, response = client.mail(sender)
        except Exception as exc:
            # Before MAIL FROM the message cannot have left this process.
            return SendOutcome(
                status=SendStatus.ABORTED,
                stage="mail",
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
        if not 200 <= code < 300:
            return SendOutcome(
                status=SendStatus.FAILED,
                stage="mail",
                code=code,
                response=_trim(response),
                enhanced=enhanced_status(_trim(response)),
            )

        try:
            code, response = client.rcpt(recipient)
        except Exception as exc:
            # A real attempt started (MAIL FROM was accepted) but the relay
            # never accepted the message body, so this is failed, not uncertain.
            return SendOutcome(
                status=SendStatus.FAILED,
                stage="rcpt",
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
        if not 200 <= code < 300:
            response_text = _trim(response)
            return SendOutcome(
                status=classify_rcpt(code, response_text),
                stage="rcpt",
                code=code,
                response=response_text,
                enhanced=enhanced_status(response_text),
            )

        try:
            code, response = client.data(raw)
        except smtplib.SMTPDataError as exc:
            response_text = _trim(getattr(exc, "smtp_error", ""))
            return SendOutcome(
                status=SendStatus.FAILED,
                stage="data",
                code=int(getattr(exc, "smtp_code", 0)) or None,
                response=response_text,
                enhanced=enhanced_status(response_text),
            )
        except Exception as exc:
            # The terminating "." may already have been accepted by the relay.
            return SendOutcome(
                status=SendStatus.UNCERTAIN,
                stage="data",
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )

        response_text = _trim(response)
        return SendOutcome(
            status=classify_data(code, response_text),
            stage="data",
            code=code,
            response=response_text,
            enhanced=enhanced_status(response_text),
        )

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        import asyncio

        try:
            await asyncio.to_thread(client.quit)
        except Exception:
            # Delivery is already classified; teardown failures change nothing.
            pass


class FakeTransport:
    """Offline transport used by the tests and dry-run harness never uses."""

    def __init__(
        self,
        outcomes: Sequence[SendOutcome | Exception] | None = None,
        *,
        open_error: Exception | None = None,
    ) -> None:
        self.outcomes = list(outcomes or [])
        self.open_error = open_error
        self.opened = 0
        self.closed = 0
        self.calls: list[tuple[str, str, bytes, str]] = []
        self.hooks: list[Callable[[], None]] = []

    async def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.opened += 1

    async def deliver(
        self,
        *,
        sender: str,
        recipient: str,
        raw: bytes,
        message_id: str,
    ) -> SendOutcome:
        self.calls.append((sender, recipient, raw, message_id))
        for hook in self.hooks:
            hook()
        if not self.outcomes:
            return SendOutcome(status=SendStatus.SENT, stage="data", code=250, response="250 OK")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        self.closed += 1
