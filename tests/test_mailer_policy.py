from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from forgesmith.config import settings
from forgesmith.mailer.models import SendConfigError
from forgesmith.mailer.policy import (
    MailPolicy,
    SendingWindow,
    delay_seconds,
    ensure_utc,
    is_allowlisted,
    is_freemail,
    load_zone,
    mask_email,
    parse_allowlist,
    recipient_domain,
    retry_not_before,
    warsaw_day_bounds,
)

WARSAW = "Europe/Warsaw"


def _warsaw(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=load_zone(WARSAW))


def test_zone_loads_with_tzdata_installed() -> None:
    assert load_zone(WARSAW).key == WARSAW


def test_invalid_timezone_has_actionable_error() -> None:
    with pytest.raises(SendConfigError, match="tzdata"):
        load_zone("Mars/Olympus")


def test_window_boundaries_are_start_inclusive_end_exclusive() -> None:
    window = SendingWindow(zone=load_zone(WARSAW), start_hour=9, end_hour=17)
    assert window.is_open(_warsaw(2026, 10, 22, 9, 0).astimezone(UTC))
    assert window.is_open(_warsaw(2026, 10, 22, 16, 59).astimezone(UTC))
    assert not window.is_open(_warsaw(2026, 10, 22, 8, 59).astimezone(UTC))
    assert not window.is_open(_warsaw(2026, 10, 22, 17, 0).astimezone(UTC))


def test_weekend_is_closed_and_next_opening_is_monday() -> None:
    window = SendingWindow(zone=load_zone(WARSAW), start_hour=9, end_hour=17)
    saturday = _warsaw(2026, 10, 24, 12, 0).astimezone(UTC)
    assert not window.is_open(saturday)
    opening = window.next_opening(saturday)
    assert opening == _warsaw(2026, 10, 26, 9, 0).astimezone(UTC)


def test_dst_edges_keep_the_window_correct() -> None:
    window = SendingWindow(zone=load_zone(WARSAW), start_hour=9, end_hour=17)
    # 2026-03-29: clocks jump 02:00 -> 03:00 CEST. 09:00 exists once.
    assert window.is_open(_warsaw(2026, 3, 30, 9, 0).astimezone(UTC))
    # 2026-10-25: fall back; 09:00 CET is 08:00 UTC.
    assert window.is_open(_warsaw(2026, 10, 26, 9, 0).astimezone(UTC))
    assert _warsaw(2026, 10, 26, 9, 0).astimezone(UTC).hour == 8
    assert _warsaw(2026, 10, 22, 23, 30).astimezone(UTC) == datetime(
        2026, 10, 22, 21, 30, tzinfo=UTC
    )


def test_day_boundary_is_local_midnight_converted_to_utc() -> None:
    zone = load_zone(WARSAW)
    start, end = warsaw_day_bounds(datetime(2026, 10, 22, 21, 30, tzinfo=UTC), zone)
    assert start == datetime(2026, 10, 21, 22, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 22, 22, 0, tzinfo=UTC)


def test_invalid_window_is_rejected() -> None:
    with pytest.raises(SendConfigError, match="window"):
        SendingWindow(zone=load_zone(WARSAW), start_hour=17, end_hour=9)
    with pytest.raises(SendConfigError, match="window"):
        SendingWindow(zone=load_zone(WARSAW), start_hour=9, end_hour=9)


def test_policy_rejects_inverted_delay_band(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mail_delay_min", 100.0)
    monkeypatch.setattr(settings, "mail_delay_max", 10.0)
    with pytest.raises(SendConfigError, match="MAIL_DELAY_MIN"):
        MailPolicy.from_settings(settings)


def test_delay_is_clamped_and_seeded() -> None:
    low = delay_seconds(random.Random(1), mean=0, sigma=0, minimum=120, maximum=1800)
    high = delay_seconds(random.Random(2), mean=10_000, sigma=1, minimum=120, maximum=1800)
    assert low == 120
    assert high == 1800
    again = delay_seconds(random.Random(1), mean=0, sigma=0, minimum=120, maximum=1800)
    assert low == again


def test_backoff_grows_exponentially_and_caps() -> None:
    first = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    assert retry_not_before(first, 1, base=300, cap=3600) == first + timedelta(seconds=300)
    assert retry_not_before(first, 2, base=300, cap=3600) == first + timedelta(seconds=600)
    assert retry_not_before(first, 9, base=300, cap=3600) == first + timedelta(seconds=3600)
    assert retry_not_before(None, 2) is None
    assert retry_not_before(first, 0) is None


def test_ensure_utc_treats_naive_as_utc() -> None:
    naive = datetime(2026, 10, 22, 9, 0)
    assert ensure_utc(naive) == datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    assert ensure_utc(None) is None


def test_allowlist_matches_exact_email_and_registrable_domain() -> None:
    allowlist = parse_allowlist("Jan@Example.com, sklep.example.com.pl")
    assert is_allowlisted("jan@example.com", "example.com", allowlist)
    assert is_allowlisted("biuro@sklep.example.com.pl", "sklep.example.com.pl", allowlist)
    assert is_allowlisted("cokolwiek@a.pl", "a.pl", parse_allowlist(""))
    assert not is_allowlisted("biuro@other.pl", "other.pl", allowlist)


def test_freemail_and_recipient_domain() -> None:
    assert is_freemail("gmail.com")
    assert is_freemail("wp.pl")
    assert is_freemail("poczta.onet.pl")
    assert not is_freemail("sklep.pl")
    assert recipient_domain("biuro@sklep.example.com.pl") == "example.com.pl"


def test_masking_hides_the_local_part() -> None:
    assert mask_email("biuro@sklep.pl") == "b***@sklep.pl"
    assert mask_email("not-an-email") == "***"
