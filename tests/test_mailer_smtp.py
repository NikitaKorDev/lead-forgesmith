from __future__ import annotations

import smtplib
from types import SimpleNamespace

import pytest

from forgesmith.mailer.models import SendStatus, TransportError
from forgesmith.mailer.transport import (
    SmtpTransport,
    enhanced_status,
    is_hard_mailbox_rejection,
)


class FakeSMTP:
    def __init__(
        self,
        *,
        mail: tuple[int, bytes] = (250, b"OK"),
        rcpt: tuple[int, bytes] = (250, b"OK"),
        data: tuple[int, bytes] = (250, b"queued"),
        mail_error: Exception | None = None,
        data_error: Exception | None = None,
        login_error: Exception | None = None,
    ) -> None:
        self.mail_response = mail
        self.rcpt_response = rcpt
        self.data_response = data
        self.mail_error = mail_error
        self.data_error = data_error
        self.login_error = login_error
        self.calls: list[str] = []

    def ehlo(self):
        self.calls.append("ehlo")
        return (250, b"OK")

    def starttls(self, context=None):
        self.calls.append("starttls")
        return (220, b"ready")

    def login(self, user, password):
        self.calls.append("login")
        if self.login_error:
            raise self.login_error
        return (235, b"auth ok")

    def mail(self, sender):
        self.calls.append(f"mail:{sender}")
        if self.mail_error:
            raise self.mail_error
        return self.mail_response

    def rcpt(self, recipient):
        self.calls.append(f"rcpt:{recipient}")
        return self.rcpt_response

    def data(self, raw):
        self.calls.append(f"data:{len(raw)}")
        if self.data_error:
            raise self.data_error
        return self.data_response

    def quit(self):
        self.calls.append("quit")
        return (221, b"bye")


def _config(**overrides):
    options = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_password": "",
        "smtp_ssl": False,
        "smtp_starttls": False,
        "smtp_timeout": 5.0,
    }
    options.update(overrides)
    return SimpleNamespace(**options)


async def _deliver(fake: FakeSMTP, cfg=None):
    transport = SmtpTransport(cfg or _config(), client_factory=lambda: fake)
    await transport.open()
    try:
        return await transport.deliver(
            sender="jan@example.com",
            recipient="biuro@sklep.pl",
            raw=b"Subject: test\r\n\r\nbody",
            message_id="<id@example.com>",
        )
    finally:
        await transport.close()


async def test_250_after_data_is_sent() -> None:
    fake = FakeSMTP()
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.SENT
    assert outcome.stage == "data"
    assert fake.calls[-1] == "quit"


async def test_starttls_and_login_are_used_when_configured() -> None:
    fake = FakeSMTP()
    cfg = _config(smtp_starttls=True, smtp_user="jan@example.com", smtp_password="secret")
    outcome = await _deliver(fake, cfg)

    assert outcome.status == SendStatus.SENT
    assert fake.calls[:4] == ["ehlo", "starttls", "ehlo", "login"]


async def test_auth_failure_is_fatal_before_reservation() -> None:
    fake = FakeSMTP(login_error=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
    transport = SmtpTransport(
        _config(smtp_starttls=True, smtp_user="jan@example.com", smtp_password="wrong"),
        client_factory=lambda: fake,
    )
    with pytest.raises(TransportError):
        await transport.open()


async def test_connect_failure_is_fatal() -> None:
    def exploding_factory():
        raise OSError("connection refused")

    transport = SmtpTransport(_config(), client_factory=exploding_factory)
    with pytest.raises(TransportError, match="smtp open failed"):
        await transport.open()


async def test_5_1_1_at_rcpt_is_a_bounce() -> None:
    fake = FakeSMTP(rcpt=(550, b"550 5.1.1 mailbox unavailable"))
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.BOUNCED
    assert outcome.enhanced == "5.1.1"


async def test_5_1_2_at_rcpt_is_a_bounce() -> None:
    fake = FakeSMTP(rcpt=(550, b"550 5.1.2 domain unavailable"))
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.BOUNCED
    assert outcome.enhanced == "5.1.2"


async def test_bare_550_is_failed_not_bounced() -> None:
    fake = FakeSMTP(rcpt=(550, b"550 rejected"))
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.FAILED
    assert is_hard_mailbox_rejection(550, "550 rejected") is True


async def test_5_7_1_is_failed_with_enhanced_status() -> None:
    fake = FakeSMTP(data=(554, b"554 5.7.1 message rejected"))
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.FAILED
    assert outcome.enhanced == "5.7.1"


async def test_4xx_at_mail_is_failed() -> None:
    fake = FakeSMTP(mail=(451, b"451 4.7.1 try later"))
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.FAILED
    assert outcome.code == 451


async def test_421_at_mail_is_failed_and_runner_aborts() -> None:
    fake = FakeSMTP(mail=(421, b"421 service unavailable"))
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.FAILED
    assert outcome.stage == "mail"
    assert outcome.code == 421


async def test_connection_lost_at_mail_is_aborted() -> None:
    fake = FakeSMTP(mail_error=smtplib.SMTPServerDisconnected("gone"))
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.ABORTED


async def test_connection_lost_after_the_dot_is_uncertain() -> None:
    fake = FakeSMTP(data_error=smtplib.SMTPServerDisconnected("gone after dot"))
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.UNCERTAIN
    assert outcome.stage == "data"


async def test_data_error_reply_is_failed_not_uncertain() -> None:
    fake = FakeSMTP(data_error=smtplib.SMTPDataError(554, b"554 5.7.1 rejected"))
    outcome = await _deliver(fake)
    assert outcome.status == SendStatus.FAILED
    assert outcome.code == 554


async def test_enhanced_status_parser() -> None:
    assert enhanced_status("550 5.1.1 nope") == "5.1.1"
    assert enhanced_status("no code here") is None
    assert enhanced_status(None) is None
