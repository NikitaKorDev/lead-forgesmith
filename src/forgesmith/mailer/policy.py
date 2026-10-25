"""Sending policy: windows, caps, pacing, backoff and allowlists.

Everything here is pure and clock-injectable so DST and boundary behaviour can
be tested offline with a fixed ``now``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from forgesmith.domain import is_freemail as _is_freemail
from forgesmith.domain import registrable_domain
from forgesmith.mailer.models import SendConfigError


def ensure_utc(value: datetime | None) -> datetime | None:
    """Normalize a read-back timestamp to aware UTC.

    SQLite returns ``DateTime(timezone=True)`` columns as naive values, and
    Python refuses aware-minus-naive arithmetic. Treating a naive value as UTC
    is correct because every writer binds aware UTC (see ``warsaw_day_bounds``).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def load_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SendConfigError(
            f"cannot load timezone {name!r}; install tzdata (uv sync) or set "
            "FORGESMITH_MAIL_WINDOW_TZ"
        ) from exc


def warsaw_day_bounds(now_utc: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC instants bounding the current local calendar day.

    The boundary is converted to UTC *before* it reaches SQL, because SQLite
    stores the wall-clock part and drops the offset.
    """
    local = ensure_utc(now_utc).astimezone(zone)
    start_local = datetime.combine(local.date(), time(0), tzinfo=zone)
    end_local = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=zone)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SendingWindow:
    zone: ZoneInfo
    start_hour: int = 9
    end_hour: int = 17
    weekdays_only: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.start_hour < self.end_hour <= 24:
            raise SendConfigError(
                "mail window must satisfy 0 <= MAIL_WINDOW_START < "
                "MAIL_WINDOW_END <= 24 (cross-midnight is not supported)"
            )

    def is_open(self, now_utc: datetime) -> bool:
        local = ensure_utc(now_utc).astimezone(self.zone)
        if self.weekdays_only and local.weekday() >= 5:
            return False
        return self.start_hour <= local.hour < self.end_hour

    def next_opening(self, now_utc: datetime) -> datetime:
        """Next instant the window opens, returned as aware UTC."""
        local = ensure_utc(now_utc).astimezone(self.zone)
        for offset in range(0, 8):
            day = local.date() + timedelta(days=offset)
            if self.weekdays_only and day.weekday() >= 5:
                continue
            candidate = datetime.combine(day, time(self.start_hour), tzinfo=self.zone)
            if candidate > local:
                return candidate.astimezone(UTC)
        # Unreachable for a valid window, but keeps the return type honest.
        return (local + timedelta(days=7)).astimezone(UTC)

    def is_weekday(self, day: date) -> bool:
        return not self.weekdays_only or day.weekday() < 5


def delay_seconds(
    rng: random.Random,
    *,
    mean: float = 540.0,
    sigma: float = 180.0,
    minimum: float = 120.0,
    maximum: float = 1800.0,
) -> float:
    """Gaussian jitter clamped to a sane band."""
    return max(minimum, min(maximum, rng.gauss(mean, sigma)))


def retry_not_before(
    last_failed_at: datetime | None,
    failed_attempts: int,
    *,
    base: float = 300.0,
    cap: float = 3600.0,
) -> datetime | None:
    """Per-message exponential backoff gate."""
    last = ensure_utc(last_failed_at)
    if last is None or failed_attempts <= 0:
        return None
    exponent = max(0, failed_attempts - 1)
    return last + timedelta(seconds=min(base * (2**exponent), cap))


def parse_allowlist(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    entries = {piece.strip().lower() for piece in raw.split(",")}
    return frozenset(entry for entry in entries if entry)


def is_allowlisted(
    email: str,
    domain: str | None,
    allowlist: frozenset[str],
) -> bool:
    """Empty allowlist allows everyone; otherwise exact email or domain match."""
    if not allowlist:
        return True
    normalized = email.strip().lower()
    if normalized in allowlist:
        return True
    candidates = {
        registrable_domain(domain) if domain else None,
        recipient_domain(normalized),
    }
    candidates.discard(None)
    for entry in allowlist:
        entry_domain = registrable_domain(entry) if "@" not in entry else None
        if entry_domain is not None and entry_domain in candidates:
            return True
    return False


def recipient_domain(email: str) -> str | None:
    if "@" not in email:
        return None
    return registrable_domain(email.rsplit("@", 1)[-1])


def is_freemail(domain: str | None) -> bool:
    """Backward-compatible alias; the canonical predicate lives in ``domain``."""
    return _is_freemail(domain)


def mask_email(email: str) -> str:
    """``biuro@sklep.pl`` -> ``b***@sklep.pl`` for info-level logs."""
    normalized = (email or "").strip()
    if "@" not in normalized:
        return "***"
    local, domain = normalized.split("@", 1)
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


@dataclass(frozen=True, slots=True)
class MailPolicy:
    window: SendingWindow
    daily_cap: int = 20
    max_attempts: int = 3
    backoff_base: float = 300.0
    backoff_cap: float = 3600.0
    delay_mean: float = 540.0
    delay_sigma: float = 180.0
    delay_min: float = 120.0
    delay_max: float = 1800.0
    allowlist: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, cfg: object) -> MailPolicy:
        zone = load_zone(str(getattr(cfg, "mail_window_tz", "Europe/Warsaw")))
        window = SendingWindow(
            zone=zone,
            start_hour=int(getattr(cfg, "mail_window_start", 9)),
            end_hour=int(getattr(cfg, "mail_window_end", 17)),
            weekdays_only=bool(getattr(cfg, "mail_weekdays_only", True)),
        )
        minimum = float(getattr(cfg, "mail_delay_min", 120.0))
        maximum = float(getattr(cfg, "mail_delay_max", 1800.0))
        if minimum > maximum:
            raise SendConfigError("MAIL_DELAY_MIN must not exceed MAIL_DELAY_MAX")
        return cls(
            window=window,
            daily_cap=int(getattr(cfg, "mail_daily_cap", 20)),
            max_attempts=int(getattr(cfg, "mail_max_attempts", 3)),
            backoff_base=float(getattr(cfg, "mail_backoff_base", 300.0)),
            backoff_cap=float(getattr(cfg, "mail_backoff_cap", 3600.0)),
            delay_mean=float(getattr(cfg, "mail_delay_mean", 540.0)),
            delay_sigma=float(getattr(cfg, "mail_delay_sigma", 180.0)),
            delay_min=minimum,
            delay_max=maximum,
            allowlist=parse_allowlist(getattr(cfg, "mail_allowlist", "")),
        )

    def delay(self, rng: random.Random) -> float:
        return delay_seconds(
            rng,
            mean=self.delay_mean,
            sigma=self.delay_sigma,
            minimum=self.delay_min,
            maximum=self.delay_max,
        )

    def next_retry_at(
        self, last_failed_at: datetime | None, failed_attempts: int
    ) -> datetime | None:
        return retry_not_before(
            last_failed_at,
            failed_attempts,
            base=self.backoff_base,
            cap=self.backoff_cap,
        )
