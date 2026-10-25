from __future__ import annotations

from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default

import pytest

from forgesmith.mailer.mime import (
    build_email,
    dry_run_path,
    live_path,
    make_message_id,
    render_bytes,
    write_eml,
)
from forgesmith.mailer.models import SendConfigError

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
MESSAGE_ID = "<test.123@example.com>"


def _message(**overrides):
    options = {
        "subject": "Pomysł na uproszczenie oferty",
        "body": "Dzień dobry,\n\nTreść po polsku: żółć.\n",
        "sender_name": "Jan Testowy",
        "sender_address": "jan@example.com",
        "recipient": "biuro@sklep.pl",
        "message_id": MESSAGE_ID,
        "date": NOW,
    }
    options.update(overrides)
    return build_email(**options)


def test_headers_and_polish_encoding() -> None:
    raw = render_bytes(_message(subject="Pomysł na współpracę – żółć"))
    parsed = message_from_bytes(raw, policy=default)

    assert parsed["From"] == "Jan Testowy <jan@example.com>"
    assert parsed["To"] == "biuro@sklep.pl"
    assert parsed["Message-ID"] == MESSAGE_ID
    assert "Pomysł na współpracę" in parsed["Subject"]
    assert parsed["Bcc"] is None
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_content_charset() == "utf-8"


def test_body_is_quoted_printable_and_round_trips() -> None:
    body = "Dzień dobry,\n\nZażółć gęślą jaźń.\n"
    raw = render_bytes(_message(body=body))

    assert b"Content-Transfer-Encoding: quoted-printable" in raw
    parsed = message_from_bytes(raw, policy=default)
    assert parsed.get_content().replace("\r\n", "\n") == body


def test_body_is_sent_verbatim_with_footer_exactly_once() -> None:
    body = "Dzień dobry,\n\nTreść.\n\nPozdrawiam,\nJan\nJeśli ta wiadomość nie jest dla Państwa, usuń kontakt."
    raw = render_bytes(_message(body=body))
    parsed = message_from_bytes(raw, policy=default)
    text = parsed.get_content()

    assert text.count("Pozdrawiam,") == 1
    assert text.count("Jeśli ta wiadomość nie jest dla Państwa") == 1


def test_header_injection_is_rejected() -> None:
    with pytest.raises(SendConfigError, match="subject"):
        _message(subject="Hello\r\nBcc: victim@example.com")
    with pytest.raises(SendConfigError, match="email"):
        _message(recipient="biuro@sklep.pl\nBcc: victim@example.com")
    with pytest.raises(SendConfigError, match="ASCII"):
        _message(recipient="biuró@sklep.pl")
    with pytest.raises(SendConfigError, match="email"):
        _message(recipient="not-an-email")


def test_eml_paths_and_atomic_write(tmp_path) -> None:
    dry = dry_run_path(tmp_path, 7, 11)
    live = live_path(tmp_path, 7, 11, 3)

    assert dry.name == "lead7-msg11.eml"
    assert live.name == "lead7-msg11-s3.eml"

    raw = render_bytes(_message())
    write_eml(dry, b"first", overwrite=True)
    write_eml(dry, raw, overwrite=True)
    assert dry.read_bytes() == raw
    assert list(tmp_path.glob("*.tmp")) == []

    write_eml(live, raw, overwrite=False)
    with pytest.raises(FileExistsError):
        write_eml(live, raw, overwrite=False)


def test_message_id_is_unique_and_uses_sender_domain() -> None:
    left = make_message_id("jan@example.com", NOW)
    right = make_message_id("jan@example.com", NOW)
    assert left != right
    assert left.endswith("@example.com>")
