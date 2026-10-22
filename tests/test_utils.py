from __future__ import annotations

import json

from forgesmith.utils import (
    extract_json_object,
    extract_json_value,
    json_payloads,
    strip_tags,
    walk_dicts,
)


def test_extract_object_with_braces_inside_strings() -> None:
    text = 'var x = {"title": "krzesło {nie} zamek", "n": 1};'
    assert extract_json_object(text, 0) == {"title": "krzesło {nie} zamek", "n": 1}


def test_extract_object_handles_escaped_quotes() -> None:
    text = r'x = {"a": "he said \"hi\"", "b": [1, 2]}'
    assert extract_json_object(text, 0) == {"a": 'he said "hi"', "b": [1, 2]}


def test_extract_double_encoded_state() -> None:
    state = {"listing": {"ads": [{"id": 1, "title": "Pufa"}]}}
    literal = json.dumps(json.dumps(state, ensure_ascii=False))
    html = f"<script>window.__PRERENDERED_STATE__= {literal};</script>"
    payloads = json_payloads(html, ("__PRERENDERED_STATE__",))
    assert payloads == [state]


def test_extract_value_prefers_plain_object() -> None:
    html = '<script id="__NEXT_DATA__" type="application/json">{"props": {"a": 1}}</script>'
    payloads = json_payloads(html, ("__NEXT_DATA__",))
    assert payloads == [{"props": {"a": 1}}]


def test_json_payloads_ignores_garbage() -> None:
    assert (
        json_payloads("window.__PRERENDERED_STATE__ = undefined;", ("__PRERENDERED_STATE__",)) == []
    )


def test_extract_json_value_returns_none_when_marker_last() -> None:
    assert extract_json_value("__X__", 0) is None


def test_walk_dicts_visits_nested() -> None:
    payload = {"a": {"b": [{"c": 1}]}}
    found = list(walk_dicts(payload))
    assert {"c": 1} in found


def test_strip_tags_collapses_whitespace() -> None:
    assert strip_tags("<b>Meble</b>\n  <i>dębowe</i>") == "Meble dębowe"
