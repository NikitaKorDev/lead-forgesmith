from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx
from httpx import Response

from forgesmith.compose.llm import OllamaClient
from forgesmith.compose.models import LLMError


@respx.mock
async def test_ollama_client_posts_json_mode_and_returns_content() -> None:
    route = respx.post("http://localhost:11434/api/chat").mock(
        return_value=Response(200, json={"message": {"content": '{"subject": "x"}'}})
    )
    client = OllamaClient(base_url="http://localhost:11434", model="qwen2.5:7b")

    try:
        content = await client.complete(system="system", user="user")
    finally:
        await client.aclose()

    assert content == '{"subject": "x"}'
    request = route.calls[0].request
    body = request.read().decode("utf-8")
    assert '"format":"json"' in body
    assert '"stream":false' in body


@respx.mock
async def test_ollama_client_maps_http_errors_to_llm_error() -> None:
    respx.post("http://localhost:11434/api/chat").mock(return_value=Response(500, text="boom"))
    client = OllamaClient(base_url="http://localhost:11434", model="qwen2.5:7b")

    try:
        with pytest.raises(LLMError, match="ollama:HTTPStatusError"):
            await client.complete(system="system", user="user")
    finally:
        await client.aclose()


@respx.mock
async def test_ollama_client_rejects_missing_or_empty_message() -> None:
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=Response(200, json={"message": {"content": "  "}})
    )
    client = OllamaClient(base_url="http://localhost:11434", model="qwen2.5:7b")

    try:
        with pytest.raises(LLMError, match="empty_message"):
            await client.complete(system="system", user="user")
    finally:
        await client.aclose()


@respx.mock
async def test_ollama_client_handles_transport_errors() -> None:
    respx.post("http://localhost:11434/api/chat").mock(side_effect=httpx.ConnectError("down"))
    client = OllamaClient(base_url="http://localhost:11434", model="qwen2.5:7b")

    try:
        with pytest.raises(LLMError, match="ConnectError"):
            await client.complete(system="system", user="user")
    finally:
        await client.aclose()


def test_client_from_settings_reads_compose_options() -> None:
    cfg = SimpleNamespace(
        ollama_base_url="http://ollama:11434/",
        ollama_model="qwen2.5:14b",
        ollama_timeout=30.0,
        compose_temperature=0.2,
        compose_num_predict=900,
    )

    client = OllamaClient.from_settings(cfg)

    assert client.base_url == "http://ollama:11434"
    assert client.model == "qwen2.5:14b"
    assert client.timeout == 30.0
    assert client.temperature == 0.2
    assert client.num_predict == 900
