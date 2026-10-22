"""Small HTML/JSON parsing helpers shared by the sources."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_TRANSLIT = str.maketrans({"ł": "l", "Ł": "L"})


def slugify(value: str) -> str:
    """'Meble dębowe' -> 'meble-debowe' (handles Polish diacritics)."""
    normalized = unicodedata.normalize("NFKD", value.translate(_TRANSLIT).lower())
    ascii_text = normalized.encode("ascii", "ignore").decode()
    return _NON_SLUG.sub("-", ascii_text).strip("-")


def strip_tags(html: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", html)).strip()


def _skip_string(text: str, index: int) -> int:
    """Given text[index] == '"', return the index just past the closing quote."""
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        index += 1
    return index


def extract_json_object(text: str, start: int) -> Any | None:
    """Parse the first complete JSON object/array starting at or after ``start``.

    Hand-written bracket matching, because marketplace HTML embeds JSON next to
    JavaScript and a greedy regex would swallow the rest of the document.
    """
    index = start
    while index < len(text) and text[index] not in "{[":
        index += 1
    if index >= len(text):
        return None

    opening = text[index]
    closing = "}" if opening == "{" else "]"
    depth = 0
    cursor = index
    while cursor < len(text):
        char = text[cursor]
        if char == '"':
            cursor = _skip_string(text, cursor)
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[index : cursor + 1])
                except ValueError:
                    return None
        cursor += 1
    return None


def extract_json_value(text: str, start: int) -> Any | None:
    """Parse a JS value at/after ``start``: JSON object, or a JSON-encoded string.

    OLX assigns ``window.__PRERENDERED_STATE__ = "{\\"listing\\":...}"`` — the
    payload is double-encoded, so the string literal has to be decoded first.
    """
    index = start
    while index < len(text) and text[index] in " \t\r\n=":
        index += 1
    if index >= len(text):
        return None
    if text[index] == '"':
        end = _skip_string(text, index)
        try:
            inner = json.loads(text[index:end])
        except ValueError:
            return extract_json_object(text, index)
        try:
            return json.loads(inner)
        except ValueError:
            return extract_json_object(text, index)
    return extract_json_object(text, index)


def json_payloads(html: str, markers: tuple[str, ...]) -> list[Any]:
    """Pull every JSON blob that follows one of ``markers`` in ``html``."""
    payloads: list[Any] = []
    for marker in markers:
        position = html.find(marker)
        while position != -1:
            blob = extract_json_value(html, position + len(marker))
            if blob is not None:
                payloads.append(blob)
            position = html.find(marker, position + len(marker))
    return payloads


def walk_dicts(node: Any, *, max_depth: int = 12) -> Iterator[dict[str, Any]]:
    """Yield every dict nested inside a JSON structure."""
    if max_depth <= 0:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_dicts(value, max_depth=max_depth - 1)
    elif isinstance(node, list):
        for item in node:
            yield from walk_dicts(item, max_depth=max_depth - 1)


def first_str(node: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty string (or dict with 'value') for ``keys``."""
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            inner = value.get("value") or value.get("name") or value.get("label")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None
