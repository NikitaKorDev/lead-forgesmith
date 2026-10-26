"""The per-country outreach gate: the table, the refusal rules and the windows.

Every country the operator can mail has exactly one row here. The tests are
exhaustive over ``POLICIES`` because a typo in a code is invisible at runtime:
the lead simply gets refused with a sentence nobody reads until a send run is
empty.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forgesmith.mailer.policy import (
    COUNTRY_ZONES,
    SendingWindow,
    load_zone,
    parse_countries,
)
from forgesmith.outreach import (
    POLICIES,
    ColdEmailBasis,
    CountryPolicy,
    policy_for,
    refusal_reason,
)

# 11:00 in Warsaw (CET) on a Monday; 06:00 in New York (EDT).
WINTER_MONDAY = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------- policy table
@pytest.mark.parametrize("code", sorted(POLICIES))
def test_every_policy_row_is_well_formed(code: str) -> None:
    policy = POLICIES[code]

    assert isinstance(policy, CountryPolicy)
    assert policy.code == code
    assert len(code) == 2 and code.isascii() and code.isupper()
    assert policy.name and policy.name != code
    assert isinstance(policy.basis, ColdEmailBasis)
    assert policy.note
    assert policy_for(code.lower()) == policy


def test_the_table_covers_the_sources_matrix() -> None:
    by_basis: dict[ColdEmailBasis, set[str]] = {}
    for policy in POLICIES.values():
        by_basis.setdefault(policy.basis, set()).add(policy.code)

    assert by_basis[ColdEmailBasis.CONSENT_REQUIRED] == {"DE", "AT", "CA", "PL"}
    assert by_basis[ColdEmailBasis.ALLOWED] == {"FR", "NL", "DK", "SE", "NO", "FI", "GB", "US"}
    # Every enum member is in use: a leftover "gray" category would invite a
    # future country to be treated as mailable without a legal basis.
    assert set(by_basis) == set(ColdEmailBasis)
    assert POLICIES["GB"].sole_trader_is_consumer is True
    assert POLICIES["DE"].sole_trader_is_consumer is True
    assert POLICIES["PL"].sole_trader_is_consumer is True
    assert POLICIES["US"].sole_trader_is_consumer is False


def test_unknown_and_blank_codes_have_no_policy() -> None:
    assert policy_for(None) is None
    assert policy_for("") is None
    assert policy_for("  ") is None
    assert policy_for("ZZ") is None
    assert policy_for("POL") is None


# -------------------------------------------------------------------- refusals
def test_a_lead_without_a_country_is_told_to_backfill() -> None:
    reason = refusal_reason(None, countries=frozenset({"PL"}))

    assert reason is not None
    assert "forgesmith db backfill-country" in reason
    assert refusal_reason("  ", countries=frozenset({"PL"})) == reason


def test_a_country_without_a_policy_row_is_refused() -> None:
    assert refusal_reason("ZZ", countries=frozenset({"ZZ"})) == "no outreach policy for country ZZ"


def test_a_consent_country_is_refused_even_when_it_is_listed() -> None:
    reason = refusal_reason("DE", countries=frozenset({"PL", "DE"}))

    assert reason == "DE requires prior consent — cold email is not enabled for it"
    # Listing it is a config error, not an override.
    assert refusal_reason("AT", countries=frozenset({"AT"})) is not None
    assert refusal_reason("CA", countries=frozenset({"CA"})) is not None
    assert refusal_reason("PL", countries=frozenset({"PL"})) is not None


def test_poland_is_refused_even_though_it_is_the_home_market() -> None:
    reason = refusal_reason("PL", countries=frozenset({"PL"}))

    assert reason is not None
    assert "prior consent" in reason


def test_a_policy_country_outside_mail_countries_is_refused() -> None:
    assert (
        refusal_reason("US", countries=frozenset({"PL"})) == "country US is not in mail_countries"
    )
    assert refusal_reason("GB", countries=frozenset()) == "country GB is not in mail_countries"


def test_a_listed_mailable_country_is_permitted() -> None:
    assert refusal_reason("GB", countries=frozenset({"GB"})) is None
    assert refusal_reason("US", countries=frozenset({"PL", "US"})) is None
    assert refusal_reason("gb", countries=frozenset({"GB"})) is None


# ------------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, frozenset()),
        ("", frozenset()),
        ("   ", frozenset()),
        ("pl", frozenset({"PL"})),
        (" Pl , de ", frozenset({"PL", "DE"})),
        ("pl,de,PL", frozenset({"PL", "DE"})),
        ("pl,,de", frozenset({"PL", "DE"})),
        ("POL,12,p1", frozenset()),
        ("xx", frozenset({"XX"})),
    ],
)
def test_parse_countries(raw: str | None, expected: frozenset[str]) -> None:
    assert parse_countries(raw) == expected


# -------------------------------------------------------------------- windows
def test_each_policy_country_has_a_zone() -> None:
    assert set(COUNTRY_ZONES) == set(POLICIES)
    for code, key in COUNTRY_ZONES.items():
        assert load_zone(key).key == key, code


def test_for_country_moves_the_window_into_the_recipient_zone() -> None:
    configured = SendingWindow(zone=load_zone("Europe/Warsaw"), start_hour=9, end_hour=17)

    assert configured.for_country("PL") is configured
    assert configured.for_country("US").zone.key == "America/New_York"
    assert configured.for_country("us").zone.key == "America/New_York"
    assert configured.for_country("GB").zone.key == "Europe/London"
    # Unknown or missing: keep the configured zone; the refusal gate runs first.
    assert configured.for_country("ZZ") is configured
    assert configured.for_country(None) is configured


def test_the_same_instant_can_be_open_locally_and_closed_abroad() -> None:
    configured = SendingWindow(zone=load_zone("Europe/Warsaw"), start_hour=9, end_hour=17)

    assert configured.for_country("PL").is_open(WINTER_MONDAY)
    assert configured.for_country("DE").is_open(WINTER_MONDAY)
    # 06:00 in New York: too early to mail a US business.
    assert not configured.for_country("US").is_open(WINTER_MONDAY)
    assert configured.for_country("US").next_opening(WINTER_MONDAY) == datetime(
        2026, 10, 26, 13, 0, tzinfo=UTC
    )

    # 14:00 UTC: New York opens (10:00) while Warsaw is one hour from closing.
    afternoon = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
    assert configured.for_country("US").is_open(afternoon)
    assert configured.for_country("PL").is_open(afternoon)
    assert not configured.for_country("PL").is_open(
        datetime(2026, 10, 26, 17, 0, tzinfo=UTC)
    )
