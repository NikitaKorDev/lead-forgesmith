"""Mailbox access: a read-only IMAP adapter plus an offline fake.

``imaplib`` is blocking, so every network call runs in a worker thread. The
adapter only ever uses ``EXAMINE`` (``select(readonly=True)``) and ``BODY.PEEK``;
``BODY[]``/``RFC822`` would set ``\\Seen`` in a mailbox a human still reads.
"""

from __future__ import annotations

import asyncio
import imaplib
import re
from collections.abc import Callable, Sequence
from datetime import date
from typing import Protocol

from forgesmith.config import Settings
from forgesmith.inbox.models import InboxConfigError, InboxError, RawMessage
from forgesmith.inbox.parse import headers_want_body

_UID = re.compile(r"\bUID (\d+)")
_SIZE = re.compile(r"RFC822\.SIZE (\d+)")


class InboxSource(Protocol):
    """Everything the runner needs from a mailbox."""

    async def open(self) -> None: ...

    async def mailbox(self) -> tuple[str, int]: ...

    async def fetch(
        self, *, after_uid: int, limit: int, since: date | None
    ) -> list[RawMessage]: ...

    async def close(self) -> None: ...


def _extract(data: Sequence[object], kind: str) -> tuple[bytes | None, int | None, int]:
    """Pull ``(payload, uid, size)`` out of an imaplib FETCH response.

    imaplib returns a list mixing tuples ``(b'1 (UID 7 ... BODY[HEADER] {n}', payload)``
    with trailing ``b')'`` markers. The literal is labeled ``BODY[HEADER]`` even
    though ``BODY.PEEK`` was requested.
    """
    payload = bytearray()
    uid: int | None = None
    size = 0
    found = False
    for item in data or ():
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        meta, chunk = item
        meta_text = meta.decode("ascii", "ignore") if isinstance(meta, bytes) else str(meta)
        match = _UID.search(meta_text)
        if match:
            uid = int(match.group(1))
        match = _SIZE.search(meta_text)
        if match:
            size = int(match.group(1))
        if f"BODY[{kind}]" in meta_text:
            found = True
            if isinstance(chunk, bytes):
                payload.extend(chunk)
    return (bytes(payload) if found else None), uid, size


class ImapInbox:
    """Read-only IMAP source. ``client_factory`` is the offline test seam."""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        self.settings = settings
        self._factory = client_factory
        self._client: object | None = None
        self._uid_validity = 0
        self._mailbox_key = f"{settings.imap_host}/{settings.imap_user}/{settings.imap_mailbox}"

    # ------------------------------------------------------------ lifecycle
    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        cfg = self.settings
        if not cfg.imap_configured:
            raise InboxConfigError(
                "IMAP is not configured: set FORGESMITH_IMAP_HOST, "
                "FORGESMITH_IMAP_USER and FORGESMITH_IMAP_PASSWORD"
            )
        if not cfg.imap_ssl and not cfg.imap_starttls:
            raise InboxConfigError(
                "refusing to send IMAP credentials over plaintext: "
                "enable FORGESMITH_IMAP_SSL or FORGESMITH_IMAP_STARTTLS"
            )
        factory = self._factory
        if factory is None:
            factory = imaplib.IMAP4_SSL if cfg.imap_ssl else imaplib.IMAP4
        client = factory(cfg.imap_host, cfg.imap_port, timeout=cfg.imap_timeout)
        try:
            if cfg.imap_starttls and not cfg.imap_ssl:
                client.starttls()
            client.login(cfg.imap_user, cfg.imap_password)
            status, _ = client.select(cfg.imap_mailbox, readonly=True)
            if status != "OK":
                raise InboxError(f"IMAP EXAMINE {cfg.imap_mailbox} failed: {status}")
            status, data = client.response("UIDVALIDITY")
            if status != "UIDVALIDITY" or not data or data[0] in (None, b""):
                raise InboxError("server did not report UIDVALIDITY")
            self._uid_validity = int(data[0])
        except Exception:
            try:
                client.logout()
            except Exception:
                pass
            raise
        self._client = client

    async def mailbox(self) -> tuple[str, int]:
        return self._mailbox_key, self._uid_validity

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.logout()
        except Exception:
            pass

    # ---------------------------------------------------------------- fetch
    async def fetch(self, *, after_uid: int, limit: int, since: date | None) -> list[RawMessage]:
        return await asyncio.to_thread(self._fetch_sync, after_uid, limit, since)

    def _fetch_sync(self, after_uid: int, limit: int, since: date | None) -> list[RawMessage]:
        client = self._client
        if client is None:
            raise InboxError("IMAP source is not open")
        if after_uid > 0:
            criterion = f"UID {after_uid + 1}:*"
        elif since is not None:
            criterion = f"SINCE {since.strftime('%d-%b-%Y')}"
        else:
            criterion = "ALL"
        status, data = client.uid("search", None, criterion)
        if status != "OK":
            raise InboxError(f"IMAP UID SEARCH failed: {status}")
        raw = data[0] if data else b""
        tokens = raw.split() if isinstance(raw, (bytes, bytearray)) else []
        uids = sorted({int(token) for token in tokens if token.isdigit()})
        # RFC 3501: ``n:*`` returns the highest UID even when ``n`` exceeds it.
        uids = [uid for uid in uids if uid > after_uid]
        if limit > 0:
            uids = uids[:limit]

        messages: list[RawMessage] = []
        for uid in uids:
            message = self._fetch_one(client, uid)
            if message is not None:
                messages.append(message)
        return messages

    def _fetch_one(self, client: object, uid: int) -> RawMessage | None:
        cfg = self.settings
        status, data = client.uid("fetch", str(uid), "(UID RFC822.SIZE BODY.PEEK[HEADER])")
        if status != "OK":
            raise InboxError(f"IMAP FETCH {uid} failed: {status}")
        headers, fetched_uid, size = _extract(data, "HEADER")
        if not headers:
            return None
        body: bytes | None = None
        if headers_want_body(headers) and (size == 0 or size <= cfg.imap_max_body_bytes):
            status, data = client.uid("fetch", str(uid), "(UID BODY.PEEK[TEXT])")
            if status == "OK":
                body, _, _ = _extract(data, "TEXT")
        return RawMessage(
            mailbox=self._mailbox_key,
            uid=fetched_uid or uid,
            uid_validity=self._uid_validity,
            headers=headers,
            body=body,
            size=size or len(headers),
        )


class FakeInbox:
    """Deterministic in-memory source used by the offline tests."""

    def __init__(
        self,
        messages: Sequence[RawMessage] = (),
        *,
        mailbox: str = "fake/INBOX",
        uid_validity: int = 1,
    ) -> None:
        self._messages = sorted(messages, key=lambda item: item.uid)
        self._mailbox = mailbox
        self._uid_validity = uid_validity
        self.opened = False
        self.closed = False
        self.fetch_calls: list[tuple[int, int, date | None]] = []

    async def open(self) -> None:
        self.opened = True

    async def mailbox(self) -> tuple[str, int]:
        return self._mailbox, self._uid_validity

    async def fetch(self, *, after_uid: int, limit: int, since: date | None) -> list[RawMessage]:
        self.fetch_calls.append((after_uid, limit, since))
        selected = [item for item in self._messages if item.uid > after_uid]
        if limit > 0:
            selected = selected[:limit]
        return selected

    async def close(self) -> None:
        self.closed = True


class FakeIMAP:
    """Minimal imaplib stand-in that records every command it receives."""

    def __init__(
        self,
        *,
        uid_validity: int = 1,
        headers: dict[int, bytes] | None = None,
        bodies: dict[int, bytes] | None = None,
        sizes: dict[int, int] | None = None,
        search_uids: list[int] | None = None,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.uid_validity = uid_validity
        self.headers = headers or {}
        self.bodies = bodies or {}
        self.sizes = sizes or {}
        self.search_uids = search_uids
        self.logged_in = False
        self.selected: tuple[str, bool] | None = None
        self.fail_select = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user))
        self.logged_in = True
        return "OK", [b"logged in"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        if self.fail_select:
            return "NO", [b"nope"]
        self.selected = (mailbox, readonly)
        return "OK", [b"1"]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        self.calls.append(("response", code))
        return "UIDVALIDITY", [str(self.uid_validity).encode("ascii")]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self.calls.append(("uid", command, *args))
        if command == "search":
            criterion = str(args[-1])
            if self.search_uids is not None:
                uids = list(self.search_uids)
            elif criterion.upper().startswith("UID "):
                start = int(criterion.split()[1].split(":")[0])
                uids = sorted(uid for uid in self.headers if uid >= start)
            else:
                uids = sorted(self.headers)
            return "OK", [" ".join(str(uid) for uid in uids).encode("ascii")]
        if command == "fetch":
            uid = int(str(args[0]))
            item = str(args[1])
            if "HEADER" in item:
                payload = self.headers.get(uid, b"")
                size = self.sizes.get(uid, len(payload))
                meta = (f"1 (UID {uid} RFC822.SIZE {size} BODY[HEADER] {{{len(payload)}}}").encode(
                    "ascii"
                )
                return "OK", [(meta, payload), b")"]
            payload = self.bodies.get(uid, b"")
            meta = f"1 (UID {uid} BODY[TEXT] {{{len(payload)}}}".encode("ascii")
            return "OK", [(meta, payload), b")"]
        return "BAD", [b"unsupported"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout",))
        self.logged_in = False
        return "BYE", [b"bye"]

    def starttls(self) -> tuple[str, list[bytes]]:
        self.calls.append(("starttls",))
        return "OK", [b"tls"]

    # --- assertions helpers -------------------------------------------------
    def commands(self) -> list[str]:
        return [str(call[1]) for call in self.calls if call and call[0] == "uid"]

    def mutations(self) -> list[str]:
        forbidden = {"store", "expunge", "move", "copy", "append", "delete"}
        return [command for command in self.commands() if command.lower() in forbidden]
