"""Per-country cold-email gate: a product decision, not legal advice.

Leads now carry a country, and the rules companies rely on differ enough that a
single global policy would be dishonest. This module is the one place where the
outreach matrix from ``sources.md`` becomes enforceable: the mailer,
``compose --check`` and the GUI all ask :func:`refusal_reason` instead of each
re-implementing a list of countries.

The matrix follows the current law rather than older guides. Poland moved to
``consent_required`` after Article 398 PKE took effect on 10 November 2024:
prior consent is required even for B2B email, and a published address is not
consent. Unknown countries are refused rather than defaulted to allowed. A
missing policy row is almost always a typo or a market the operator has not
researched; silently treating it as allowed would opt an entire country in by
accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ColdEmailBasis(StrEnum):
    """How much prior consent a country expects before a cold B2B email."""

    ALLOWED = "allowed"
    CONSENT_REQUIRED = "consent_required"


@dataclass(frozen=True, slots=True)
class CountryPolicy:
    code: str
    name: str
    basis: ColdEmailBasis
    #: Informational until a lead carries a legal form. The gate refuses every
    #: consent country regardless of this flag, so it can never be an override.
    sole_trader_is_consumer: bool
    note: str


_ROLE_RELEVANT = "allowed if relevant to the recipient's role and with opt-out"
_CONSENT = "generally needs prior consent, even B2B; phone also restricted"

#: Kept in sync with ``sources.md`` and the current law. When in doubt, refuse:
#: an unknown or newly restricted country must never default to allowed.
POLICIES: dict[str, CountryPolicy] = {
    "AT": CountryPolicy(
        code="AT",
        name="Austria",
        basis=ColdEmailBasis.CONSENT_REQUIRED,
        sole_trader_is_consumer=True,
        note=_CONSENT,
    ),
    "CA": CountryPolicy(
        code="CA",
        name="Canada",
        basis=ColdEmailBasis.CONSENT_REQUIRED,
        sole_trader_is_consumer=False,
        note="CASL: consent required in most cases",
    ),
    "DE": CountryPolicy(
        code="DE",
        name="Germany",
        basis=ColdEmailBasis.CONSENT_REQUIRED,
        sole_trader_is_consumer=True,
        note=_CONSENT,
    ),
    "DK": CountryPolicy(
        code="DK",
        name="Denmark",
        basis=ColdEmailBasis.ALLOWED,
        sole_trader_is_consumer=False,
        note=_ROLE_RELEVANT,
    ),
    "FI": CountryPolicy(
        code="FI",
        name="Finland",
        basis=ColdEmailBasis.ALLOWED,
        sole_trader_is_consumer=False,
        note=_ROLE_RELEVANT,
    ),
    "FR": CountryPolicy(
        code="FR",
        name="France",
        basis=ColdEmailBasis.ALLOWED,
        sole_trader_is_consumer=False,
        note=_ROLE_RELEVANT,
    ),
    "GB": CountryPolicy(
        code="GB",
        name="United Kingdom",
        basis=ColdEmailBasis.ALLOWED,
        sole_trader_is_consumer=True,
        note="OK for limited companies; sole traders/partnerships treated like consumers",
    ),
    "NL": CountryPolicy(
        code="NL",
        name="Netherlands",
        basis=ColdEmailBasis.ALLOWED,
        sole_trader_is_consumer=False,
        note=_ROLE_RELEVANT,
    ),
    "NO": CountryPolicy(
        code="NO",
        name="Norway",
        basis=ColdEmailBasis.ALLOWED,
        sole_trader_is_consumer=False,
        note=_ROLE_RELEVANT,
    ),
    "PL": CountryPolicy(
        code="PL",
        name="Poland",
        basis=ColdEmailBasis.CONSENT_REQUIRED,
        sole_trader_is_consumer=True,
        note=(
            "art. 398 PKE (10 Nov 2024): prior consent required for commercial "
            "email, including B2B and sole traders; a published address is not consent"
        ),
    ),
    "SE": CountryPolicy(
        code="SE",
        name="Sweden",
        basis=ColdEmailBasis.ALLOWED,
        sole_trader_is_consumer=False,
        note=_ROLE_RELEVANT,
    ),
    "US": CountryPolicy(
        code="US",
        name="United States",
        basis=ColdEmailBasis.ALLOWED,
        sole_trader_is_consumer=False,
        note="CAN-SPAM: honest sender details and opt-out",
    ),
}


def policy_for(code: str | None) -> CountryPolicy | None:
    """Look up a country by ISO-3166-1 alpha-2 code; ``None`` when unknown."""
    normalized = (code or "").strip().upper()
    return POLICIES.get(normalized)


def refusal_reason(code: str | None, *, countries: frozenset[str]) -> str | None:
    """``None`` when a send is permitted, else one operator-facing sentence.

    ``countries`` is the configured ``mail_countries`` set. A country that
    requires consent is refused even when it appears there: listing it is a
    configuration mistake, not an override, and the gate must not let one
    typo turn a consent country live.
    """
    normalized = (code or "").strip().upper()
    if not normalized:
        return "lead has no country; run `forgesmith db backfill-country`"
    policy = POLICIES.get(normalized)
    if policy is None:
        return f"no outreach policy for country {normalized}"
    if policy.basis is ColdEmailBasis.CONSENT_REQUIRED:
        return f"{normalized} requires prior consent — cold email is not enabled for it"
    if normalized not in countries:
        return f"country {normalized} is not in mail_countries"
    return None
