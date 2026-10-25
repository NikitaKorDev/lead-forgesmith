"""Sequential send runner: select, preflight, reserve, deliver, finalize.

The reservation transaction is authoritative. Selection is only an
optimization; every check from the Phase 4 plan is repeated inside the
``reserve`` call, after the SMTP connection is known to work.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import structlog

from forgesmith.compose.models import ComposeConfigError
from forgesmith.compose.prompt import FOOTER_VERSION, SenderProfile
from forgesmith.config import Settings
from forgesmith.config import settings as default_settings
from forgesmith.db import MailRepo, init_db, session_scope
from forgesmith.db.models import Event
from forgesmith.mailer.lock import RunLock
from forgesmith.mailer.mime import (
    build_email,
    dry_run_path,
    live_path,
    make_message_id,
    render_bytes,
    resolve_sender,
    write_eml,
)
from forgesmith.mailer.models import (
    QueuedMessage,
    RecheckResult,
    Reservation,
    SendConfigError,
    SendRefused,
    SendStats,
    SendStatus,
    TransportError,
)
from forgesmith.mailer.policy import MailPolicy, ensure_utc, mask_email, warsaw_day_bounds
from forgesmith.mailer.transport import MailTransport, SendOutcome, SmtpTransport

log = structlog.get_logger(__name__)

Now = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]
Confirmer = Callable[[Sequence[QueuedMessage]], bool]
Stopper = Callable[[], bool]
Clock = Callable[[], float]

#: Cooperative stop checks this often inside the inter-send jitter sleep. Short
#: enough that SIGTERM is honoured inside a container grace period, long enough
#: to keep the injected-sleep seam simple.
STOP_POLL_SECONDS = 5.0


class SendRunner:
    def __init__(
        self,
        transport: MailTransport | None = None,
        *,
        settings: Settings | None = None,
        sender: SenderProfile | None = None,
        now: Now | None = None,
        sleep: Sleeper | None = None,
        rng: random.Random | None = None,
        clock: Clock | None = None,
        acquire_lock: bool = True,
    ) -> None:
        self.transport = transport
        self.settings = settings or default_settings
        self.sender = sender
        self.now: Now = now or (lambda: datetime.now(UTC))
        self.sleep: Sleeper = sleep or asyncio.sleep
        self.rng = rng or random.Random()
        self.clock: Clock = clock or time.monotonic
        self.acquire_lock = acquire_lock

    async def run(
        self,
        *,
        limit: int | None = None,
        lead_id: int | None = None,
        source: str | None = None,
        live: bool = False,
        no_delay: bool = False,
        force_window: bool = False,
        confirm: Confirmer | None = None,
        stop: Stopper | None = None,
        max_runtime: float = 0.0,
    ) -> SendStats:
        cfg = self.settings
        stats = SendStats()
        started = self.clock()

        # Kill switch, checked before sender/policy config so a paused pipeline
        # never fails for unrelated reasons. A *dry* run still works while it is
        # armed; only a requested live run is refused.
        if live and cfg.mail_dry_run:
            stats.stop_reason = "kill_switch"
            log.warning(
                "mailer.kill_switch",
                hint="FORGESMITH_MAIL_DRY_RUN=true blocks live sends",
            )
            return self._finish(stats, started)

        policy = MailPolicy.from_settings(cfg)
        sender = self.sender or _sender_from_settings(cfg)
        sender_name, sender_address = resolve_sender(sender)
        budget = max_runtime or cfg.send_max_runtime

        transport: MailTransport | None = None
        lock: RunLock | None = None
        try:
            if live:
                transport = self.transport or SmtpTransport(cfg)
                if self.acquire_lock:
                    lock = RunLock(cfg.mailer_lock_path)
                    lock.acquire()
                async with session_scope(cfg) as session:
                    repo = MailRepo(session)
                    stats.uncertain_recovered = await repo.recover_pending(now=self.now())
                    stats.uncertain_outstanding = await repo.count_uncertain()

            async with session_scope(cfg) as session:
                snapshots = await MailRepo(session).select_queued(
                    lead_id=lead_id, source=source, limit=limit
                )
            stats.scanned = len(snapshots)
            if not snapshots:
                return self._finish(stats, started)

            if live and confirm is not None and not confirm(snapshots):
                raise SendRefused("live run was not confirmed")

            if not live:
                await self._run_dry(snapshots, sender, policy, stats)
                return self._finish(stats, started)

            # Pre-open checks: do not even connect when the run is certain to
            # send nothing. These are repeated per message inside the
            # reservation, which remains authoritative.
            now = ensure_utc(self.now()) or datetime.now(UTC)
            if not force_window and not policy.window.is_open(now):
                stats.stop_reason = "outside_window"
                log.info(
                    "mailer.window_closed.preflight",
                    next_opening=policy.window.next_opening(now).isoformat(),
                )
                return self._finish(stats, started)
            day_start, day_end = warsaw_day_bounds(now, policy.window.zone)
            async with session_scope(cfg) as session:
                sent_today = await MailRepo(session).count_sent_between(day_start, day_end)
            if sent_today >= policy.daily_cap:
                stats.stop_reason = "daily_cap"
                log.info("mailer.daily_cap.preflight", cap=policy.daily_cap)
                return self._finish(stats, started)

            assert transport is not None
            await transport.open()  # fatal before anything is reserved
            await self._run_live(
                snapshots,
                transport=transport,
                sender=sender,
                sender_name=sender_name,
                sender_address=sender_address,
                policy=policy,
                stats=stats,
                no_delay=no_delay,
                force_window=force_window,
                stop=stop,
                max_runtime=budget,
            )
            return self._finish(stats, started)
        except TransportError as exc:
            log.error("mailer.transport_fatal", error=str(exc))
            raise SendConfigError(str(exc)) from exc
        finally:
            if transport is not None:
                await transport.close()
            if lock is not None:
                lock.release()

    def _finish(self, stats: SendStats, started: float) -> SendStats:
        stats.duration_s = round(self.clock() - started, 2)
        return stats

    async def _run_dry(
        self,
        snapshots: Sequence[QueuedMessage],
        sender: SenderProfile,
        policy: MailPolicy,
        stats: SendStats,
    ) -> None:
        outbox = self.settings.outbox_dir
        for snapshot in snapshots:
            now = ensure_utc(self.now()) or datetime.now(UTC)
            async with session_scope(self.settings) as session:
                result = await MailRepo(session).preview(
                    snapshot,
                    now=now,
                    expected_footer_version=FOOTER_VERSION,
                    max_attempts=policy.max_attempts,
                    backoff_base=policy.backoff_base,
                    backoff_cap=policy.backoff_cap,
                )
            if not result.ok:
                stats.skipped += 1
                log.info(
                    "mailer.dry_skipped",
                    lead_id=snapshot.lead_id,
                    reason=result.reason,
                )
                continue
            try:
                raw = self._render(snapshot, sender, now=now)
            except SendConfigError as exc:
                stats.skipped += 1
                log.warning("mailer.mime_invalid", lead_id=snapshot.lead_id, error=str(exc))
                continue
            path = dry_run_path(outbox, snapshot.lead_id, snapshot.message_id)
            try:
                write_eml(path, raw, overwrite=True)
            except OSError as exc:
                stats.skipped += 1
                stats.stop_reason = "archive_failed"
                log.error(
                    "mailer.archive_failed",
                    lead_id=snapshot.lead_id,
                    path=str(path),
                    error=str(exc),
                )
                break
            async with session_scope(self.settings) as session:
                session.add(
                    Event(
                        entity="message",
                        entity_id=snapshot.message_id,
                        action="dry_run",
                        detail={
                            "lead_id": snapshot.lead_id,
                            "to_email": snapshot.to_email,
                            "path": str(path),
                        },
                    )
                )
            stats.dry_run += 1
            log.info(
                "mailer.dry_run",
                lead_id=snapshot.lead_id,
                recipient=mask_email(snapshot.to_email),
                path=str(path),
            )

    async def _run_live(
        self,
        snapshots: Sequence[QueuedMessage],
        *,
        transport: MailTransport,
        sender: SenderProfile,
        sender_name: str,
        sender_address: str,
        policy: MailPolicy,
        stats: SendStats,
        no_delay: bool,
        force_window: bool,
        stop: Stopper | None = None,
        max_runtime: float = 0.0,
    ) -> None:
        consecutive_policy_errors = 0
        runtime_start = self.clock()
        for index, snapshot in enumerate(snapshots):
            if stop is not None and stop():
                stats.stop_reason = "signal"
                log.info("mailer.stop_requested", stage="pre_send")
                break
            if max_runtime and self.clock() - runtime_start >= max_runtime:
                stats.stop_reason = "budget"
                log.info("mailer.budget_exhausted", budget_s=max_runtime)
                break
            now = ensure_utc(self.now()) or datetime.now(UTC)
            window_open = force_window or policy.window.is_open(now)
            if not window_open:
                stats.stop_reason = "outside_window"
                log.info(
                    "mailer.window_closed",
                    next_opening=policy.window.next_opening(now).isoformat(),
                )
                break

            start, end = warsaw_day_bounds(now, policy.window.zone)
            async with session_scope(self.settings) as session:
                sent_today = await MailRepo(session).count_sent_between(start, end)
            under_cap = sent_today < policy.daily_cap
            if not under_cap:
                stats.stop_reason = "daily_cap"
                log.info("mailer.daily_cap_reached", cap=policy.daily_cap)
                break

            message_id = make_message_id(sender_address, now)
            try:
                raw = self._render(snapshot, sender, now=now, message_id=message_id)
            except SendConfigError as exc:
                stats.skipped += 1
                log.warning("mailer.mime_invalid", lead_id=snapshot.lead_id, error=str(exc))
                continue

            async with session_scope(self.settings) as session:
                reservation = await MailRepo(session).reserve(
                    snapshot,
                    smtp_message_id=message_id,
                    now=now,
                    expected_footer_version=FOOTER_VERSION,
                    max_attempts=policy.max_attempts,
                    backoff_base=policy.backoff_base,
                    backoff_cap=policy.backoff_cap,
                    allowlist=policy.allowlist,
                    window_open=window_open,
                    under_cap=under_cap,
                )
            if isinstance(reservation, RecheckResult):
                if reservation.defer or reservation.reason == "retry_not_before":
                    stats.deferred += 1
                else:
                    stats.skipped += 1
                log.info(
                    "mailer.recheck_skipped",
                    lead_id=snapshot.lead_id,
                    reason=reservation.reason,
                )
                if reservation.stop:
                    stats.stop_reason = reservation.reason
                    break
                continue

            assert isinstance(reservation, Reservation)
            archive = live_path(
                self.settings.outbox_dir,
                snapshot.lead_id,
                snapshot.message_id,
                reservation.send_id,
            )
            try:
                write_eml(archive, raw, overwrite=False)
            except OSError as exc:
                # The live .eml is the audit evidence for this attempt. Without
                # it we refuse to transmit, and the reservation becomes
                # ``aborted`` so no attempt is consumed and nothing is retried.
                async with session_scope(self.settings) as session:
                    await MailRepo(session).finalize(
                        reservation.send_id,
                        status=SendStatus.ABORTED,
                        stage="archive",
                        detail=f"archive_failed: {type(exc).__name__}: {exc}",
                        now=self.now(),
                    )
                stats.aborted += 1
                stats.stop_reason = "archive_failed"
                log.error(
                    "mailer.archive_failed",
                    lead_id=snapshot.lead_id,
                    send_id=reservation.send_id,
                    path=str(archive),
                    error=str(exc),
                )
                break

            try:
                outcome = await transport.deliver(
                    sender=sender_address,
                    recipient=reservation.to_email,
                    raw=raw,
                    message_id=reservation.smtp_message_id,
                )
            except Exception as exc:
                # A transport may raise instead of returning a stage-tagged
                # outcome. We cannot tell how far it got, so assume the relay
                # might already hold the message: ``uncertain``, never retried.
                log.error(
                    "mailer.deliver_unexpected",
                    lead_id=snapshot.lead_id,
                    send_id=reservation.send_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                outcome = SendOutcome(
                    status=SendStatus.UNCERTAIN,
                    stage="deliver",
                    detail=f"{type(exc).__name__}: {exc}"[:200],
                )
            async with session_scope(self.settings) as session:
                await MailRepo(session).finalize(
                    reservation.send_id,
                    status=outcome.status,
                    stage=outcome.stage,
                    code=outcome.code,
                    response=outcome.response,
                    detail=outcome.detail,
                    now=self.now(),
                )
            self._count(stats, outcome)
            log.info(
                "mailer.delivery",
                lead_id=snapshot.lead_id,
                send_id=reservation.send_id,
                recipient=mask_email(reservation.to_email),
                status=outcome.status,
                stage=outcome.stage,
                code=outcome.code,
            )

            if outcome.status == SendStatus.ABORTED:
                stats.stop_reason = "aborted"
                break
            if outcome.status == SendStatus.UNCERTAIN and outcome.stage == "deliver":
                # The transport failed in an unclassifiable way. Assume the
                # message may have left and stop: the next recipient must not
                # be turned into collateral uncertainty.
                stats.stop_reason = "transport_error"
                break
            if outcome.stage == "mail" and outcome.code == 421:
                stats.stop_reason = "relay_unavailable"
                break
            if outcome.enhanced and outcome.enhanced.startswith("5.7."):
                consecutive_policy_errors += 1
            else:
                consecutive_policy_errors = 0
            if consecutive_policy_errors >= 2:
                stats.stop_reason = "policy_rejection_streak"
                log.warning("mailer.policy_streak_abort")
                break

            if not no_delay and index + 1 < len(snapshots):
                delay = policy.delay(self.rng)
                if max_runtime:
                    remaining = max_runtime - (self.clock() - runtime_start)
                    if remaining <= 0:
                        stats.stop_reason = "budget"
                        break
                    delay = min(delay, remaining)
                if await self._sleep_with_stop(delay, stop):
                    stats.stop_reason = "signal"
                    log.info("mailer.stop_requested", stage="jitter")
                    break

    async def _sleep_with_stop(self, seconds: float, stop: Stopper | None) -> bool:
        """Sleep ``seconds``, checking ``stop`` at most every 5 s.

        Returns ``True`` when a stop was requested. Never called while a
        delivery is in flight: a signal only takes effect between messages.
        """
        if stop is None:
            await self.sleep(seconds)
            return False
        remaining = seconds
        while remaining > 0:
            if stop():
                return True
            step = min(STOP_POLL_SECONDS, remaining)
            await self.sleep(step)
            remaining -= step
        return stop()

    @staticmethod
    def _count(stats: SendStats, outcome: SendOutcome) -> None:
        mapping = {
            SendStatus.SENT: "sent",
            SendStatus.FAILED: "failed",
            SendStatus.BOUNCED: "bounced",
            SendStatus.UNCERTAIN: "uncertain",
            SendStatus.ABORTED: "aborted",
        }
        field = mapping.get(outcome.status)
        if field:
            setattr(stats, field, getattr(stats, field) + 1)

    def _render(
        self,
        snapshot: QueuedMessage,
        sender: SenderProfile,
        *,
        now: datetime,
        message_id: str | None = None,
    ) -> bytes:
        sender_name, sender_address = resolve_sender(sender)
        message = build_email(
            subject=snapshot.subject,
            body=snapshot.body,
            sender_name=sender_name,
            sender_address=sender_address,
            recipient=snapshot.to_email,
            message_id=message_id or make_message_id(sender_address, now),
            date=now,
        )
        return render_bytes(message)


def _sender_from_settings(cfg: Settings) -> SenderProfile:
    try:
        return SenderProfile.from_settings(cfg)
    except ComposeConfigError as exc:
        raise SendConfigError(str(exc)) from exc


async def send_queued(
    *,
    limit: int | None = None,
    lead_id: int | None = None,
    source: str | None = None,
    live: bool = False,
    no_delay: bool = False,
    force_window: bool = False,
    confirm: Confirmer | None = None,
    stop: Stopper | None = None,
    max_runtime: float = 0.0,
    settings: Settings | None = None,
    transport: MailTransport | None = None,
    sender: SenderProfile | None = None,
    now: Now | None = None,
    sleep: Sleeper | None = None,
    rng: random.Random | None = None,
    clock: Clock | None = None,
) -> SendStats:
    """Public entry point used by the CLI and tests."""
    cfg = settings or default_settings
    await init_db(cfg)
    runner = SendRunner(
        transport,
        settings=cfg,
        sender=sender,
        now=now,
        sleep=sleep,
        rng=rng,
        clock=clock,
    )
    return await runner.run(
        limit=limit,
        lead_id=lead_id,
        source=source,
        live=live,
        no_delay=no_delay,
        force_window=force_window,
        confirm=confirm,
        stop=stop,
        max_runtime=max_runtime,
    )
