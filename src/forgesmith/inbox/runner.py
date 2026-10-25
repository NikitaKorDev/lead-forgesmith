"""One ingest pass: read the mailbox, commit one transaction per message.

Correctness rests on the two unique constraints plus the compare-and-swap in
:meth:`InboxRepo.apply_inbound`, not on the advisory lock. The lock exists so a
second concurrent sync fails loudly instead of hammering the provider.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import structlog
from sqlalchemy.exc import IntegrityError

from forgesmith.config import Settings
from forgesmith.config import settings as default_settings
from forgesmith.db import InboxRepo, init_db, session_scope
from forgesmith.db.models import utcnow
from forgesmith.inbox.models import InboundKind, InboundStatus, SyncStats
from forgesmith.inbox.parse import parse_message
from forgesmith.inbox.source import ImapInbox, InboxSource
from forgesmith.mailer.lock import RunLock
from forgesmith.mailer.policy import ensure_utc

log = structlog.get_logger("forgesmith.inbox")


async def sync_inbox(
    *,
    settings: Settings | None = None,
    source: InboxSource | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    now: datetime | None = None,
) -> SyncStats:
    """Fetch new messages and apply the safe outcomes.

    ``dry_run`` fetches and parses but writes nothing and does not move the
    cursor. Every other run commits the ledger row, at most one transition and
    the cursor advance in a single transaction per message.
    """
    cfg = settings or default_settings
    started = time.monotonic()
    stats = SyncStats()
    stamp = ensure_utc(now) or utcnow()
    lock = RunLock(cfg.inbox_lock_path)
    lock.acquire()
    inbox = source or ImapInbox(cfg)
    try:
        await init_db(cfg)
        await inbox.open()
        mailbox, uid_validity = await inbox.mailbox()

        async with session_scope(cfg) as session:
            repo = InboxRepo(session)
            if dry_run:
                # A dry run must not create or touch the cursor row.
                state = await repo.get_mailbox(mailbox)
                after_uid = state.last_uid if state is not None else 0
                reset = state is not None and state.uid_validity != uid_validity
            else:
                state, reset = await repo.prepare_mailbox(mailbox, uid_validity, now=stamp)
                after_uid = state.last_uid
        if reset:
            after_uid = 0
            stats.uidvalidity_reset = 1
            log.warning("inbox.uidvalidity_changed", mailbox=mailbox, uid_validity=uid_validity)

        since = stamp.date() - timedelta(days=cfg.imap_lookback_days) if after_uid == 0 else None
        fetch_limit = limit if limit is not None else cfg.imap_fetch_limit
        messages = await inbox.fetch(after_uid=after_uid, limit=fetch_limit, since=since)

        for raw in messages:
            stats.scanned += 1
            try:
                parsed = parse_message(raw.raw, size=raw.size)
            except Exception as exc:
                stats.failed += 1
                log.warning("inbox.parse_failed", uid=raw.uid, error=str(exc))
                if not dry_run:
                    async with session_scope(cfg) as session:
                        await InboxRepo(session).advance_cursor(mailbox, raw.uid, now=stamp)
                continue

            if dry_run:
                stats.dry_run += 1
                continue

            try:
                async with session_scope(cfg) as session:
                    repo = InboxRepo(session)
                    row = await repo.record_inbound(parsed, raw, now=stamp)
                    if row is None:
                        stats.duplicates += 1
                    else:
                        stats.ingested += 1
                        if repo.should_auto_apply(parsed, row) and await repo.apply_inbound(
                            row, now=stamp
                        ):
                            stats.applied += 1
                        if parsed.kind == InboundKind.DSN and parsed.is_hard_bounce:
                            stats.bounces += 1
                        elif parsed.kind == InboundKind.REPLY:
                            stats.replies += 1
                        elif row.status == InboundStatus.NEEDS_REVIEW:
                            stats.needs_review += 1
                        elif row.status == InboundStatus.IGNORED:
                            stats.ignored += 1
                    await repo.advance_cursor(mailbox, raw.uid, now=stamp)
            except IntegrityError:
                # Another process ingested this exact message first. The ledger
                # insert rolled back, so advance the cursor in a fresh transaction.
                stats.duplicates += 1
                async with session_scope(cfg) as session:
                    await InboxRepo(session).advance_cursor(mailbox, raw.uid, now=stamp)
    finally:
        try:
            await inbox.close()
        finally:
            lock.release()

    stats.duration_s = round(time.monotonic() - started, 3)
    stats.stop_reason = "completed" if stats.scanned else "empty"
    return stats
