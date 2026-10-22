from __future__ import annotations

from forgesmith.enrichment.emails import (
    deobfuscate,
    eligibility_reason,
    find_candidates,
    select_contacts,
)
from forgesmith.enrichment.site import parse_html


def page(html: str, url: str = "https://a.pl/"):
    return parse_html(html, url)


def contacts(html: str, site_domain: str = "a.pl", *, limit: int = 3):
    parsed = page(html)
    return select_contacts([("https://a.pl/", parsed)], site_domain, limit=limit)


def test_role_beats_personal_on_the_same_page() -> None:
    hits, rejected = contacts(
        """
        <footer>
          <a href="mailto:biuro@a.pl">biuro</a>
          <a href="mailto:jan.kowalski@a.pl">jan</a>
        </footer>
        """
    )
    assert [hit.email for hit in hits] == ["biuro@a.pl"]
    assert hits[0].kind == "role"
    assert hits[0].confidence == 1.0
    assert rejected == []


def test_free_provider_role_address_is_kept_and_ranked() -> None:
    hits, _ = contacts('<a href="mailto:kontakt@gmail.com">napisz</a>')
    assert [hit.email for hit in hits] == ["kontakt@gmail.com"]
    assert hits[0].kind == "role"
    assert hits[0].confidence == 0.8


def test_personal_address_is_the_only_fallback() -> None:
    hits, _ = contacts('<a href="mailto:jan.kowalski@a.pl">jan</a>')
    assert [hit.email for hit in hits] == ["jan.kowalski@a.pl"]
    assert hits[0].kind == "personal"
    assert hits[0].confidence == 0.5


def test_cap_is_three_roles() -> None:
    hits, _ = contacts(
        """
        <p>kontakt@a.pl biuro@a.pl sklep@a.pl sales@a.pl</p>
        """
    )
    assert len(hits) == 3
    assert {hit.email for hit in hits} == {"kontakt@a.pl", "biuro@a.pl", "sales@a.pl"}


def test_secondary_roles_only_used_when_no_primary_role_exists() -> None:
    hits, _ = contacts("<p>marketing@a.pl press@a.pl</p>")
    assert [hit.email for hit in hits] == ["marketing@a.pl"]

    hits, _ = contacts("<p>kontakt@a.pl marketing@a.pl press@a.pl</p>")
    assert [hit.email for hit in hits] == ["kontakt@a.pl"]


def test_obfuscated_addresses_are_deobfuscated() -> None:
    assert deobfuscate("kontakt [at] a [dot] pl") == "kontakt@a.pl"
    hits, _ = contacts("<p>kontakt (at) a (dot) pl</p>")
    assert [hit.email for hit in hits] == ["kontakt@a.pl"]


def test_hidden_honeypot_and_script_content_are_ignored() -> None:
    parsed = page(
        """
        <div style="display: none">honeypot@a.pl</div>
        <script>var email = "script@a.pl";</script>
        <p>kontakt@a.pl</p>
        """
    )
    assert find_candidates(parsed) == ["kontakt@a.pl"]


def test_block_boundary_prevents_com_wholesale_concatenation() -> None:
    parsed = page("<div>STORE@MISBH.COM</div><div>WHOLESALE</div>")
    assert find_candidates(parsed) == ["store@misbh.com"]
    hits, _ = contacts("<div>STORE@MISBH.COM</div><div>WHOLESALE</div>", "misbh.com")
    assert [hit.email for hit in hits] == ["store@misbh.com"]


def test_multiple_addresses_in_one_mailto_are_split() -> None:
    hits, _ = contacts('<a href="mailto:kontakt@a.pl,sales@a.pl?subject=hello">napisz</a>')
    assert {hit.email for hit in hits} == {"kontakt@a.pl", "sales@a.pl"}


def test_jsonld_email_is_used() -> None:
    hits, _ = contacts('<script type="application/ld+json">{"email": "kontakt@a.pl"}</script>')
    assert [hit.email for hit in hits] == ["kontakt@a.pl"]


def test_vendor_forbidden_and_file_addresses_are_rejected() -> None:
    assert eligibility_reason("sentry@wixpress.com", "a.pl") == "vendor_domain"
    assert eligibility_reason("no-reply@a.pl", "a.pl") == "forbidden_role"
    assert eligibility_reason("image@2x.png", "a.pl") == "invalid_structure"


def test_third_party_non_role_address_is_rejected() -> None:
    assert eligibility_reason("someone@other.pl", "a.pl") == "not_site_or_role"


def test_uppercase_duplicate_is_stored_once() -> None:
    hits, _ = contacts("<p>Kontakt@A.pl</p><p>kontakt@a.pl</p>")
    assert len(hits) == 1
    assert hits[0].email == "kontakt@a.pl"
