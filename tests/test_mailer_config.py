from __future__ import annotations

import pytest

from forgesmith.config import settings
from forgesmith.mailer.models import SendConfigError
from forgesmith.mailer.policy import MailPolicy
from forgesmith.mailer.runner import _sender_from_settings


def test_policy_builds_from_settings(mail_env) -> None:
    policy = MailPolicy.from_settings(mail_env)
    assert policy.window.zone.key == "Europe/Warsaw"
    assert policy.window.start_hour == 0
    assert policy.max_attempts == 3


def test_policy_parses_mail_countries(mail_env, monkeypatch) -> None:
    monkeypatch.setattr(mail_env, "mail_countries", " pl, de ")

    assert MailPolicy.from_settings(mail_env).countries == frozenset({"PL", "DE"})


def test_an_empty_mail_countries_is_an_empty_set(mail_env, monkeypatch) -> None:
    monkeypatch.setattr(mail_env, "mail_countries", "")

    assert MailPolicy.from_settings(mail_env).countries == frozenset()


def test_missing_sender_identity_is_actionable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mail_from_name", "")
    with pytest.raises(SendConfigError, match="MAIL_FROM_NAME"):
        _sender_from_settings(settings)


def test_invalid_sender_address_fails(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mail_from_name", "Jan")
    monkeypatch.setattr(settings, "mail_from_address", "jan@example.com")
    monkeypatch.setattr(settings, "sender_company", "Studio")
    monkeypatch.setattr(settings, "sender_offer", "oferta")
    assert _sender_from_settings(settings).email == "jan@example.com"

    monkeypatch.setattr(settings, "mail_from_address", "not-an-address")
    with pytest.raises(SendConfigError):
        _sender_from_settings(settings)
