"""Deterministic hard gates and advisory warnings for generated drafts."""

from __future__ import annotations

import json
import re
import unicodedata

from pydantic import ValidationError

from forgesmith.compose.models import (
    DraftParseError,
    DraftPayload,
    FactPayload,
    ValidationResult,
)
from forgesmith.compose.prompt import PROMPT_VERSION

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML = re.compile(r"<[^>]+>")
_MARKDOWN = re.compile(r"```|\*\*|__|^#{1,6}\s", re.MULTILINE)
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f]",
    flags=re.UNICODE,
)
_PLACEHOLDER = re.compile(r"\{\{[^}]+\}\}|\[[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż ]{2,30}\]")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+\b", re.IGNORECASE)
_REPLY_PREFIX = re.compile(r"^(?:re|fwd|odp|aw)\s*:", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_DIGIT = re.compile(r"\d+(?:[.,]\d+)?")
_INFORMAL = re.compile(
    r"\b(?:wasz|wasza|wasze|twoje|twój|twoja|cię|ci|tobie|zapraszam\s+cię|u\s+ciebie)\b",
    re.IGNORECASE,
)
_CLICHE = re.compile(
    r"mam\s+nadzieję|nie\s+zajmę\s+dużo\s+czasu|szybkie\s+pytanie|"
    r"chciał(?:bym|abym)\s+zaproponować\s+współpracę",
    re.IGNORECASE,
)
_PRESSURE = re.compile(
    r"100\s*%|gwarantujemy|najlepsz|pilne|ostatnia\s+szansa|tylko\s+teraz|natychmiast",
    re.IGNORECASE,
)
_SIGNATURE = re.compile(
    r"^\s*(?:pozdrawiam|z\s+poważaniem|z\s+szacunkiem|z\s+wyrazami\s+szacunku|"
    r"serdecznie\s+pozdrawiam|łączę\s+pozdrowienia)\b",
    re.IGNORECASE | re.MULTILINE,
)
_POLISH_CHARS = re.compile(r"[ąćęłńóśźż]", re.IGNORECASE)
_POLISH_WORDS = re.compile(
    r"\b(?:i|w|na|z|do|dla|się|jest|są|państwo|firmy|sklepu|oferty|kontakt)\b",
    re.IGNORECASE,
)


def parse_draft(raw: str) -> DraftPayload:
    """Parse model output, tolerating a single Markdown fence but not prose."""
    if not raw or not raw.strip():
        raise DraftParseError("empty response")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DraftParseError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise DraftParseError("response is not a JSON object")
    try:
        return DraftPayload.model_validate(data)
    except ValidationError as exc:
        raise DraftParseError(f"schema: {exc.error_count()} error(s)") from exc


def validate_draft(
    draft: DraftPayload,
    payload: FactPayload,
    footer: str,
    *,
    min_words: int = 80,
    max_words: int = 140,
) -> ValidationResult:
    result = ValidationResult()
    subject = draft.subject.strip()
    body = draft.body.strip()
    context = normalize_for_match(payload.grounding_text())
    body_norm = normalize_for_match(body)

    result.subject_length = len(subject)
    result.word_count = len(_WORD.findall(body))

    _validate_subject(subject, result)
    _validate_body(body, footer, min_words, max_words, result)
    _validate_numbers(subject, body, payload.grounding_text(), result)
    _validate_evidence(draft.evidence, context, body_norm, result)
    _collect_warnings(body, result)
    return result


def assemble_footer(body: str, footer: str) -> str:
    full = f"{body.strip()}\n\n{footer}"
    if full.count(footer) != 1:
        raise ValueError("footer_count")
    return full


def normalize_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def evidence_is_grounded(phrase: str, context_norm: str) -> bool:
    normalized = normalize_for_match(phrase)
    if len(normalized) < 12 and len(normalized.split()) < 3:
        return False
    return f" {normalized} " in f" {context_norm} "


def _validate_subject(subject: str, result: ValidationResult) -> None:
    if not 3 <= len(subject) <= 80:
        result.errors.append("subject_length")
    if _CONTROL.search(subject) or "\r" in subject or "\n" in subject:
        result.errors.append("subject_control")
    if _REPLY_PREFIX.search(subject):
        result.errors.append("subject_reply_prefix")
    if _PLACEHOLDER.search(subject):
        result.errors.append("subject_placeholder")
    if _URL.search(subject) or _EMAIL.search(subject):
        result.errors.append("subject_url_or_email")


def _validate_body(
    body: str,
    footer: str,
    min_words: int,
    max_words: int,
    result: ValidationResult,
) -> None:
    words = len(_WORD.findall(body))
    if not min_words <= words <= max_words:
        result.errors.append("body_word_count")
    if _HTML.search(body):
        result.errors.append("body_html")
    if _MARKDOWN.search(body):
        result.errors.append("body_markdown")
    if _EMOJI.search(body):
        result.errors.append("body_emoji")
    if _PLACEHOLDER.search(body):
        result.errors.append("body_placeholder")
    if _CONTROL.search(body) or "\r" in body:
        result.errors.append("body_control")
    if _URL.search(body) or _EMAIL.search(body):
        result.errors.append("body_url_or_email")
    if _SIGNATURE.search(body):
        result.errors.append("body_signature")
    if footer and footer in body:
        result.errors.append("footer_in_body")


def _validate_numbers(
    subject: str,
    body: str,
    grounding_text: str,
    result: ValidationResult,
) -> None:
    haystack = grounding_text
    for token in _DIGIT.findall(f"{subject}\n{body}"):
        token = token.replace(",", ".").rstrip(".")
        if not _token_in_haystack(token, haystack):
            result.errors.append("invented_number")
            return


def _token_in_haystack(token: str, haystack: str) -> bool:
    for candidate in _DIGIT.findall(haystack):
        candidate = candidate.replace(",", ".").rstrip(".")
        if candidate == token:
            return True
        # "1.299,00" is one number in the payload, while "1" is not grounded
        # on its own; do not loosen that boundary.
    return False


def _validate_evidence(
    evidence: list[str],
    context_norm: str,
    body_norm: str,
    result: ValidationResult,
) -> None:
    phrases = [phrase.strip() for phrase in evidence if phrase and phrase.strip()]
    if not phrases:
        result.errors.append("evidence_missing")
        return
    grounded = [phrase for phrase in phrases if evidence_is_grounded(phrase, context_norm)]
    result.grounded_evidence = grounded
    if not grounded:
        result.errors.append("evidence_not_grounded")
        return
    if not any(f" {normalize_for_match(phrase)} " in f" {body_norm} " for phrase in grounded):
        result.warnings.append("evidence_not_in_body")


def _collect_warnings(body: str, result: ValidationResult) -> None:
    if _INFORMAL.search(body):
        result.warnings.append("informal_address")
    if _CLICHE.search(body):
        result.warnings.append("cliche")
    if _PRESSURE.search(body):
        result.warnings.append("pressure_language")
    if not _POLISH_CHARS.search(body) and not _POLISH_WORDS.search(body):
        result.warnings.append("not_polish")


__all__ = [
    "PROMPT_VERSION",
    "assemble_footer",
    "evidence_is_grounded",
    "normalize_for_match",
    "parse_draft",
    "validate_draft",
]
