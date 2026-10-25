from __future__ import annotations

import json

import pytest

from forgesmith.compose.models import DraftParseError, DraftPayload, FactPayload
from forgesmith.compose.validate import (
    assemble_footer,
    evidence_is_grounded,
    normalize_for_match,
    parse_draft,
    validate_draft,
)

FOOTER = "Pozdrawiam,\nJan Testowy\nStudio Testowe\njan@example.com"
SENTENCE = "Prowadzicie sprzedaż mebli z litego drewna i tworzycie własne kolekcje."
PAYLOAD = FactPayload(
    domain="a.pl",
    source="shopify",
    company_name="A sp. z o.o.",
    storefront_platform="shopify",
    description="Tworzymy meble z litego drewna.",
    text_excerpt=SENTENCE,
    headings=["Nasza pracownia"],
    assortment_titles=["Stół dębowy"],
    contact_kind="role",
)


def _body(extra: str = "") -> str:
    return (
        "Dzień dobry,\n\n"
        + (SENTENCE + " ") * 7
        + extra
        + "Czy moglibyśmy porozmawiać o uproszczeniu zbierania danych o produktach? "
        "Zależy mi na krótkiej rozmowie."
    )


def _draft(
    *,
    subject: str = "Pomysł na uproszczenie oferty",
    body: str | None = None,
    evidence: list[str] | None = None,
) -> DraftPayload:
    return DraftPayload(
        subject=subject,
        body=body if body is not None else _body(),
        evidence=evidence if evidence is not None else ["sprzedaż mebli z litego drewna"],
    )


def test_valid_draft_passes_all_hard_gates() -> None:
    result = validate_draft(_draft(), PAYLOAD, FOOTER)

    assert result.ok is True
    assert result.errors == []
    assert 80 <= result.word_count <= 140
    assert result.grounded_evidence == ["sprzedaż mebli z litego drewna"]


def test_subject_gates() -> None:
    assert "subject_length" in validate_draft(_draft(subject="x"), PAYLOAD, FOOTER).errors
    assert (
        "subject_control"
        in validate_draft(_draft(subject="Temat\r\nBcc: x@example.com"), PAYLOAD, FOOTER).errors
    )
    assert (
        "subject_reply_prefix"
        in validate_draft(_draft(subject="Re: współpraca"), PAYLOAD, FOOTER).errors
    )
    assert (
        "subject_placeholder"
        in validate_draft(_draft(subject="Pomysł dla [Firma]"), PAYLOAD, FOOTER).errors
    )


def test_body_gates_for_markup_urls_placeholders_and_emoji() -> None:
    assert "body_html" in validate_draft(_draft(body=_body("<b>ważne</b>")), PAYLOAD, FOOTER).errors
    assert (
        "body_markdown" in validate_draft(_draft(body=_body("**ważne**")), PAYLOAD, FOOTER).errors
    )
    assert (
        "body_url_or_email"
        in validate_draft(_draft(body=_body("Zobacz https://a.pl")), PAYLOAD, FOOTER).errors
    )
    assert (
        "body_url_or_email"
        in validate_draft(_draft(body=_body("Napisz na biuro@a.pl")), PAYLOAD, FOOTER).errors
    )
    assert (
        "body_placeholder"
        in validate_draft(_draft(body=_body("Dla [Firma]")), PAYLOAD, FOOTER).errors
    )
    assert "body_emoji" in validate_draft(_draft(body=_body("💕")), PAYLOAD, FOOTER).errors


def test_word_count_and_invented_numbers_are_hard_failures() -> None:
    short = validate_draft(_draft(body="Dzień dobry, krótka wiadomość."), PAYLOAD, FOOTER)
    assert "body_word_count" in short.errors

    invented = validate_draft(_draft(body=_body("Mamy 2026 klientów.")), PAYLOAD, FOOTER)
    assert "invented_number" in invented.errors


def test_digits_must_exist_in_cleaned_payload() -> None:
    grounded = validate_draft(_draft(body=_body("Działacie od 2004 roku.")), PAYLOAD, FOOTER)
    assert "invented_number" in grounded.errors

    payload = FactPayload(
        **{**PAYLOAD.canonical_dict(), "text_excerpt": SENTENCE + " Od 2004 roku."}
    )
    grounded = validate_draft(_draft(body=_body("Działacie od 2004 roku.")), payload, FOOTER)
    assert "invented_number" not in grounded.errors


def test_evidence_must_be_grounded_and_not_too_short() -> None:
    missing = validate_draft(_draft(evidence=[]), PAYLOAD, FOOTER)
    assert "evidence_missing" in missing.errors

    short = validate_draft(_draft(evidence=["drewna"]), PAYLOAD, FOOTER)
    assert "evidence_not_grounded" in short.errors

    absent = validate_draft(_draft(evidence=["sprzedaż mebli z metalu"]), PAYLOAD, FOOTER)
    assert "evidence_not_grounded" in absent.errors


def test_evidence_not_quoted_in_body_is_only_a_warning() -> None:
    result = validate_draft(
        _draft(evidence=["Tworzymy meble z litego drewna"]),
        PAYLOAD,
        FOOTER,
    )

    assert result.ok is True
    assert "evidence_not_in_body" in result.warnings


def test_advisory_warnings_do_not_block_a_draft() -> None:
    result = validate_draft(
        _draft(body=_body("Twoja firma jest najlepsza i gwarantujemy efekt.")),
        PAYLOAD,
        FOOTER,
    )

    assert result.ok is True
    assert "informal_address" in result.warnings
    assert "pressure_language" in result.warnings


def test_model_footer_must_not_be_duplicated() -> None:
    result = validate_draft(_draft(body=_body(FOOTER)), PAYLOAD, FOOTER)
    assert "footer_in_body" in result.errors

    signature = validate_draft(
        _draft(body=_body() + "\n\nZ poważaniem,\nStudio Testowe"),
        PAYLOAD,
        FOOTER,
    )
    assert "body_signature" in signature.errors

    with pytest.raises(ValueError, match="footer_count"):
        assemble_footer(_body(FOOTER), FOOTER)


def test_parse_accepts_fenced_json_and_rejects_prose_or_schema_errors() -> None:
    raw = "```json\n" + json.dumps(_draft().model_dump(), ensure_ascii=False) + "\n```"

    assert parse_draft(raw).subject == "Pomysł na uproszczenie oferty"

    with pytest.raises(DraftParseError, match="invalid JSON"):
        parse_draft("not json")
    with pytest.raises(DraftParseError, match="schema"):
        parse_draft('{"subject": "x", "body": "y", "extra": true}')


def test_evidence_matching_is_case_and_punctuation_insensitive() -> None:
    context = normalize_for_match(SENTENCE)

    assert evidence_is_grounded("Sprzedaż mebli z litego drewna!", context) is True
    assert evidence_is_grounded("meble", context) is False
    assert evidence_is_grounded("sprzedaż mebli z metalu", context) is False
