"""NIP and legal-name extraction from already-fetched pages.

Deliberately narrow: a checksum-valid NIP with an explicit label, and a legal
name only when it sits next to that NIP or comes from JSON-LD ``legalName``.
``Lead.name`` is never a fallback — for OLX it is often a first name.
"""

from __future__ import annotations

import re

from forgesmith.enrichment.site import ParsedPage
from forgesmith.utils import walk_dicts

_NIP_LABEL = re.compile(
    r"\bNIP\b[^\d]{0,20}(?:(PL)\s*)?((?:\d[ -]?){9}\d)",
    re.IGNORECASE,
)
_LEGAL_FORM = re.compile(
    r"([A-ZĄĆĘŁŃÓŚŹŻ][\w\s\.\-&,'\"]{2,140}?"
    r"(?:spółka z ograniczoną odpowiedzialnością|sp\.?\s*z\s*o\.?\s*o\.?|"
    r"sp\.?\s*k\.?|sp\.?\s*j\.?|S\.?\s*A\.?))",
    re.IGNORECASE,
)
_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)
_LEGAL_NAME_MAX_GAP = 80
_ORGANIZATION_TYPES = {"organization", "localbusiness", "store", "onlinestore"}


def normalize_nip(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("PL") and len(digits) == 12:
        digits = digits[2:]
    return digits if len(digits) == 10 else None


def valid_nip(value: str) -> bool:
    digits = normalize_nip(value)
    if digits is None:
        return False
    checksum = sum(
        int(digit) * weight for digit, weight in zip(digits[:9], _NIP_WEIGHTS, strict=True)
    )
    return checksum % 11 == int(digits[9])


def find_nip(pages: list[tuple[str, ParsedPage]]) -> dict[str, str] | None:
    for url, page in pages:
        for match in _NIP_LABEL.finditer(page.text):
            value = normalize_nip(match.group(2))
            if value and valid_nip(value):
                start = max(0, match.start() - 60)
                end = min(len(page.text), match.end() + 60)
                return {
                    "value": value,
                    "source_url": url,
                    "snippet": " ".join(page.text[start:end].split()),
                }
    return None


def _node_types(node: dict) -> set[str]:
    value = node.get("@type")
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, list):
        return {str(item).casefold() for item in value}
    return set()


def _jsonld_legal_name(pages: list[tuple[str, ParsedPage]]) -> str | None:
    for _url, page in pages:
        for payload in page.jsonld:
            for node in walk_dicts(payload):
                if not (_node_types(node) & _ORGANIZATION_TYPES):
                    continue
                value = node.get("legalName")
                if isinstance(value, str) and 2 <= len(value.strip()) <= 200:
                    return value.strip()
    return None


def _nip_spans(page: ParsedPage, digits: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in _NIP_LABEL.finditer(page.text):
        value = normalize_nip(match.group(2))
        if value == digits and valid_nip(value):
            spans.append((match.start(), match.end()))
    return spans


def find_legal_name(pages: list[tuple[str, ParsedPage]], nip: str | None = None) -> str | None:
    from_jsonld = _jsonld_legal_name(pages)
    if from_jsonld:
        return from_jsonld
    if not nip:
        return None
    digits = normalize_nip(nip)
    if not digits:
        return None
    for _url, page in pages:
        spans = _nip_spans(page, digits)
        if not spans:
            continue
        candidates: list[tuple[int, str]] = []
        for candidate in _LEGAL_FORM.finditer(page.text):
            name = " ".join(candidate.group(1).split())
            if not 3 <= len(name) <= 200:
                continue
            for nip_start, nip_end in spans:
                if candidate.end() <= nip_start:
                    gap = nip_start - candidate.end()
                elif candidate.start() >= nip_end:
                    gap = candidate.start() - nip_end
                else:
                    gap = 0
                if gap <= _LEGAL_NAME_MAX_GAP:
                    candidates.append((gap, name))
                    break
        if candidates:
            candidates.sort(key=lambda item: (item[0], len(item[1])))
            return candidates[0][1]
    return None
