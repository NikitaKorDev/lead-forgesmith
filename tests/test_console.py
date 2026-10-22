from __future__ import annotations

import sys

from forgesmith import console as console_module


class _FakeStream:
    def __init__(self) -> None:
        self.kwargs: dict[str, str] = {}

    def reconfigure(self, **kwargs: str) -> None:
        self.kwargs = kwargs


class _BrokenStream:
    def reconfigure(self, **kwargs: str) -> None:
        raise ValueError("detached")


class _PlainStream:
    pass


def test_force_utf8_stdio_reconfigures_both_streams(monkeypatch):
    out, err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    console_module.force_utf8_stdio()

    assert out.kwargs == {"encoding": "utf-8", "errors": "replace"}
    assert err.kwargs == {"encoding": "utf-8", "errors": "replace"}


def test_force_utf8_stdio_survives_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _PlainStream())
    monkeypatch.setattr(sys, "stderr", _BrokenStream())

    console_module.force_utf8_stdio()


def test_force_utf8_stdio_survives_unreconfigurable_stream(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _BrokenStream())
    monkeypatch.setattr(sys, "stderr", _BrokenStream())

    console_module.force_utf8_stdio()
