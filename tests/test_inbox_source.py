from __future__ import annotations

import pytest

from forgesmith.config import Settings
from forgesmith.inbox.models import InboxConfigError, InboxError
from forgesmith.inbox.source import FakeIMAP, FakeInbox, ImapInbox

DSN_HEADERS = (
    b"From: MAILER-DAEMON@example.net\r\n"
    b"To: jan@example.com\r\n"
    b"Subject: Undelivered Mail Returned to Sender\r\n"
    b"Message-ID: <dsn@example.net>\r\n"
    b'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n'
)
DSN_BODY = b"--b\r\nContent-Type: message/delivery-status\r\n\r\nAction: failed\r\n\r\n--b--\r\n"
PLAIN_HEADERS = (
    b"From: anna@a.pl\r\n"
    b"To: jan@example.com\r\n"
    b"Subject: Hello\r\n"
    b"Message-ID: <plain@a.pl>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
)
PLAIN_BODY = b"Just saying hello.\r\n"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "imap_host": "imap.example.net",
        "imap_user": "jan@example.com",
        "imap_password": "secret",
        "imap_mailbox": "INBOX",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _inbox(fake: FakeIMAP, settings: Settings | None = None) -> ImapInbox:
    return ImapInbox(settings or _settings(), client_factory=lambda *a, **k: fake)


async def test_open_uses_examine_and_reports_uidvalidity() -> None:
    fake = FakeIMAP(uid_validity=42, headers={7: DSN_HEADERS})
    inbox = _inbox(fake)

    await inbox.open()
    mailbox, uid_validity = await inbox.mailbox()
    await inbox.close()

    assert mailbox == "imap.example.net/jan@example.com/INBOX"
    assert uid_validity == 42
    assert fake.selected == ("INBOX", True)
    assert ("select", "INBOX", True) in fake.calls
    assert ("logout",) in fake.calls


async def test_fetch_never_requests_a_seen_marking_form() -> None:
    fake = FakeIMAP(headers={7: DSN_HEADERS}, bodies={7: DSN_BODY})
    inbox = _inbox(fake)
    await inbox.open()
    messages = await inbox.fetch(after_uid=0, limit=10, since=None)
    await inbox.close()

    assert len(messages) == 1
    assert messages[0].uid == 7
    assert messages[0].body == DSN_BODY
    fetches = [call[3] for call in fake.calls if call[0] == "uid" and call[1] == "fetch"]
    assert fetches, "no fetch was issued"
    for item in fetches:
        assert "BODY.PEEK" in item
        assert "BODY[]" not in item
    assert fake.mutations() == []


async def test_plain_message_is_header_only() -> None:
    fake = FakeIMAP(headers={3: PLAIN_HEADERS}, bodies={3: PLAIN_BODY})
    inbox = _inbox(fake)
    await inbox.open()
    messages = await inbox.fetch(after_uid=0, limit=10, since=None)
    await inbox.close()

    assert messages[0].body is None
    assert [command for command in fake.commands()] == ["search", "fetch"]


async def test_oversized_body_is_not_downloaded() -> None:
    settings = _settings(imap_max_body_bytes=10)
    fake = FakeIMAP(
        headers={7: DSN_HEADERS},
        bodies={7: DSN_BODY},
        sizes={7: 10_000},
    )
    inbox = _inbox(fake, settings)
    await inbox.open()
    messages = await inbox.fetch(after_uid=0, limit=10, since=None)
    await inbox.close()

    assert messages[0].body is None
    assert messages[0].size == 10_000
    assert [command for command in fake.commands()] == ["search", "fetch"]


async def test_search_n_star_is_filtered_against_the_cursor() -> None:
    # RFC 3501: ``UID 11:*`` returns the highest UID (5) even though 5 < 11.
    fake = FakeIMAP(headers={5: PLAIN_HEADERS}, search_uids=[5])
    inbox = _inbox(fake)
    await inbox.open()
    messages = await inbox.fetch(after_uid=10, limit=10, since=None)
    await inbox.close()

    assert messages == []
    assert [command for command in fake.commands()] == ["search"]


async def test_bootstrap_uses_since_when_no_cursor() -> None:
    from datetime import date

    fake = FakeIMAP(headers={1: PLAIN_HEADERS})
    inbox = _inbox(fake)
    await inbox.open()
    await inbox.fetch(after_uid=0, limit=10, since=date(2026, 10, 1))
    await inbox.close()

    searches = [call for call in fake.calls if call[0] == "uid" and call[1] == "search"]
    assert searches[0][-1] == "SINCE 01-Oct-2026"


async def test_limit_caps_the_batch() -> None:
    fake = FakeIMAP(headers={1: PLAIN_HEADERS, 2: PLAIN_HEADERS, 3: PLAIN_HEADERS})
    inbox = _inbox(fake)
    await inbox.open()
    messages = await inbox.fetch(after_uid=0, limit=2, since=None)
    await inbox.close()

    assert [message.uid for message in messages] == [1, 2]


async def test_missing_configuration_fails_fast() -> None:
    inbox = _inbox(FakeIMAP(), _settings(imap_host=""))
    with pytest.raises(InboxConfigError):
        await inbox.open()


async def test_plaintext_credentials_are_refused() -> None:
    inbox = _inbox(FakeIMAP(), _settings(imap_ssl=False, imap_starttls=False))
    with pytest.raises(InboxConfigError):
        await inbox.open()


async def test_starttls_is_used_when_ssl_is_off() -> None:
    fake = FakeIMAP()
    inbox = _inbox(fake, _settings(imap_ssl=False, imap_starttls=True))
    await inbox.open()
    await inbox.close()

    assert ("starttls",) in fake.calls
    assert fake.logged_in is False  # closed


async def test_failed_select_logs_out_and_raises() -> None:
    fake = FakeIMAP()
    fake.fail_select = True
    inbox = _inbox(fake)
    with pytest.raises(InboxError):
        await inbox.open()

    assert ("logout",) in fake.calls


async def test_fake_inbox_filters_and_records_calls() -> None:
    from forgesmith.inbox.models import RawMessage

    source = FakeInbox(
        [
            RawMessage("m", 2, 1, PLAIN_HEADERS, None, len(PLAIN_HEADERS)),
            RawMessage("m", 1, 1, PLAIN_HEADERS, None, len(PLAIN_HEADERS)),
        ]
    )
    await source.open()
    assert await source.mailbox() == ("fake/INBOX", 1)
    messages = await source.fetch(after_uid=1, limit=10, since=None)
    await source.close()
    assert [message.uid for message in messages] == [2]
    assert source.fetch_calls == [(1, 10, None)]
    assert source.closed is True
