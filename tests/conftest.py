from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from forgesmith.compose.prompt import FOOTER_VERSION, PROMPT_VERSION, SenderProfile
from forgesmith.config import settings
from forgesmith.db import init_db, reset_state, session_scope
from forgesmith.db.models import Contact, Event, Lead, Message

MAIL_BODY = (
    "Dzień dobry,\n\n"
    "Prowadzą Państwo sklep z meblami i tworzą własne kolekcje z litego drewna. "
    "Tworzę rozwiązania, które pomagają zbierać dane o produktach bez ręcznego "
    "przepisywania informacji.\n\n"
    "Pozdrawiam,\nJan Testowy\nStudio Testowe\njan@example.com\n"
    "Jeśli ta wiadomość nie jest dla Państwa, proszę o krótką odpowiedź – usunę kontakt."
)


@pytest.fixture(autouse=True)
def _fast_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """No politeness delays in tests."""
    monkeypatch.setattr(settings, "request_delay", 0.0)
    monkeypatch.setattr(settings, "request_timeout", 5.0)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    )
    reset_state()
    await init_db()
    yield
    reset_state()


@pytest.fixture
def mail_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Sender identity plus a wide-open window and zero delay by default."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    # ``mail_dry_run`` is the unattended kill switch; these tests exercise the
    # live path, so they must opt out of it explicitly.
    monkeypatch.setattr(settings, "mail_dry_run", False)
    monkeypatch.setattr(settings, "mail_from_name", "Jan Testowy")
    monkeypatch.setattr(settings, "mail_from_address", "jan@example.com")
    monkeypatch.setattr(settings, "sender_company", "Studio Testowe")
    monkeypatch.setattr(settings, "sender_offer", "automatyzacja zbierania danych")
    monkeypatch.setattr(settings, "mail_allowlist", "")
    monkeypatch.setattr(settings, "mail_daily_cap", 20)
    monkeypatch.setattr(settings, "mail_window_start", 0)
    monkeypatch.setattr(settings, "mail_window_end", 24)
    monkeypatch.setattr(settings, "mail_weekdays_only", False)
    monkeypatch.setattr(settings, "mail_delay_mean", 0.0)
    monkeypatch.setattr(settings, "mail_delay_sigma", 0.0)
    monkeypatch.setattr(settings, "mail_delay_min", 0.0)
    monkeypatch.setattr(settings, "mail_delay_max", 0.0)
    monkeypatch.setattr(settings, "mail_max_attempts", 3)
    monkeypatch.setattr(settings, "mail_backoff_base", 300.0)
    monkeypatch.setattr(settings, "mail_backoff_cap", 3600.0)
    return settings


@pytest.fixture
def mail_sender() -> SenderProfile:
    return SenderProfile(
        name="Jan Testowy",
        email="jan@example.com",
        company="Studio Testowe",
        offer="automatyzacja zbierania danych",
    )


@pytest_asyncio.fixture
async def seed_queued(db, mail_env):
    """Seed a lead whose latest message is (optionally) approved."""

    async def make(
        *,
        source_id: str = "a.pl",
        domain: str | None = "a.pl",
        email: str = "biuro@a.pl",
        contact_kind: str = "role",
        lead_status: str = "queued",
        approved: bool = True,
        footer_version: str = FOOTER_VERSION,
        prompt_version: str = PROMPT_VERSION,
        body: str = MAIL_BODY,
        subject: str = "Pomysł na uproszczenie oferty",
        contact_id: int | None = None,
    ) -> SimpleNamespace:
        async with session_scope() as session:
            lead = Lead(
                source="shopify",
                source_id=source_id,
                name=f"Sklep {source_id}",
                url=f"https://{source_id}/",
                domain=domain,
                status=lead_status,
            )
            session.add(lead)
            await session.flush()
            contact = Contact(
                lead_id=lead.id,
                email=email,
                kind=contact_kind,
                confidence=0.9,
            )
            session.add(contact)
            await session.flush()
            message = Message(
                lead_id=lead.id,
                contact_id=contact.id if contact_id is None else contact_id,
                subject=subject,
                body=body,
                model="qwen2.5:7b",
                prompt_version=prompt_version,
                hooks={"footer_version": footer_version, "context_hash": "test"},
            )
            session.add(message)
            await session.flush()
            if approved:
                session.add(
                    Event(
                        entity="message",
                        entity_id=message.id,
                        action="approved",
                        detail={
                            "lead_id": lead.id,
                            "contact_id": contact.id,
                            "to_email": email.lower(),
                            "footer_version": footer_version,
                            "prompt_version": prompt_version,
                            "bulk": False,
                        },
                    )
                )
            await session.flush()
            return SimpleNamespace(
                lead_id=lead.id,
                contact_id=contact.id,
                message_id=message.id,
                email=email,
                domain=domain,
            )

    return make
