"""Small provider abstraction; Ollama is the Phase 3 default."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from forgesmith.compose.models import LLMError


class LLMClient(Protocol):
    async def complete(self, *, system: str, user: str) -> str: ...


class OllamaClient:
    """Minimal Ollama ``/api/chat`` client with JSON mode."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        temperature: float = 0.4,
        num_predict: int = 600,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.num_predict = num_predict
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls, cfg: object) -> OllamaClient:
        return cls(
            base_url=str(getattr(cfg, "ollama_base_url", "http://localhost:11434")),
            model=str(getattr(cfg, "ollama_model", "qwen2.5:7b")),
            timeout=float(getattr(cfg, "ollama_timeout", 120.0)),
            temperature=float(getattr(cfg, "compose_temperature", 0.4)),
            num_predict=int(getattr(cfg, "compose_num_predict", 600)),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def complete(self, *, system: str, user: str) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "num_predict": self.num_predict,
            },
        }
        try:
            response = await self._get_client().post(
                f"{self.base_url}/api/chat",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama:{type(exc).__name__}") from exc
        except ValueError as exc:
            raise LLMError("ollama:invalid_response") from exc

        try:
            content = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMError("ollama:missing_message") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("ollama:empty_message")
        return content

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))
        return self._client
