"""ModelClient shim: provider-agnostic chat completion over plain httpx.

Mirrors the proven provider pattern of scripts/kb_agent.py (LLM_PROVIDER /
LLM_MODEL / LLM_API_KEY / LLM_BASE_URL, OpenAI-compatible vs Anthropic branch),
deliberately duplicated into this service per ADR-0008 — never imported. The
panel needs exactly one capability (system+user -> text), so the port is one
method and the adapters are raw JSON calls; no SDK dependency.
"""

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx

from review_panel.domain.errors import ModelAPIError
from review_panel.structured_logging import get_logger

logger = get_logger("review_panel.infrastructure.model_client")

_OPENAI_COMPATIBLE = ("groq", "openai", "openai_compatible", "ollama")

_DEFAULT_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "ollama": "http://localhost:11434/v1",
    "anthropic": "https://api.anthropic.com",
}

_TIMEOUT_SECONDS = 120.0
_MAX_TOKENS = 4096


class ModelClient(Protocol):
    """The single model capability the panel needs: one completion per call."""

    async def complete(self, *, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class ModelSettings:
    provider: str
    model: str
    api_key: str
    base_url: str
    ca_cert: str | None = None


def load_model_settings(env: dict[str, str] | None = None) -> ModelSettings:
    """Read the kb_agent-style LLM_* environment. Fails fast on missing values."""
    src = env if env is not None else dict(os.environ)
    provider = src.get("LLM_PROVIDER", "groq")
    if provider not in (*_OPENAI_COMPATIBLE, "anthropic"):
        raise ModelAPIError(f"unsupported LLM_PROVIDER: {provider!r}")
    model = src.get("LLM_MODEL", "llama-3.3-70b-versatile" if provider == "groq" else "")
    if not model:
        raise ModelAPIError("LLM_MODEL is required for this provider")
    api_key = src.get("LLM_API_KEY") or src.get("GROQ_API_KEY", "")
    if not api_key and provider != "ollama":
        raise ModelAPIError("LLM_API_KEY (or GROQ_API_KEY) is required")
    base_url = src.get("LLM_BASE_URL") or _DEFAULT_BASE_URLS.get(provider, "")
    if not base_url:
        raise ModelAPIError(f"LLM_BASE_URL is required for provider {provider!r}")
    return ModelSettings(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        ca_cert=src.get("LLM_CA_CERT") or src.get("SSL_CERT_FILE") or None,
    )


class _HttpModelClient:
    """Shared httpx plumbing; `transport` is the hermetic-test seam."""

    def __init__(
        self, settings: ModelSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        verify: str | bool = self._settings.ca_cert if self._settings.ca_cert else True
        if self._transport is not None:
            return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport)
        return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, verify=verify)

    async def _post_json(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            async with self._http() as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ModelAPIError(f"model request failed: {type(exc).__name__}: {exc}") from exc
        logger.info(
            "event=model_call provider=%s model=%s status=%s latency_ms=%s",
            self._settings.provider,
            self._settings.model,
            response.status_code,
            int((time.monotonic() - started) * 1000),
        )
        if response.status_code >= 400:
            raise ModelAPIError(
                f"model endpoint returned {response.status_code}: {response.text[:500]}"
            )
        body: dict[str, Any] = response.json()
        return body


class OpenAICompatModelClient(_HttpModelClient):
    """groq / openai / any OpenAI-compatible chat-completions endpoint (incl. ollama /v1)."""

    async def complete(self, *, system: str, user: str) -> str:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "max_tokens": _MAX_TOKENS,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Authorization": f"Bearer {self._settings.api_key}"}
        body = await self._post_json(
            f"{self._settings.base_url}/chat/completions", headers, payload
        )
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelAPIError(f"unexpected chat-completions response shape: {exc}") from exc
        if not isinstance(content, str):
            raise ModelAPIError("chat-completions content is not text")
        return content


class AnthropicModelClient(_HttpModelClient):
    """Anthropic Messages API over plain httpx."""

    _API_VERSION = "2023-06-01"

    async def complete(self, *, system: str, user: str) -> str:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "max_tokens": _MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self._settings.api_key,
            "anthropic-version": self._API_VERSION,
        }
        body = await self._post_json(f"{self._settings.base_url}/v1/messages", headers, payload)
        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise ModelAPIError("unexpected messages response shape: no content list")
        parts: list[str] = []
        for block_obj in cast("list[Any]", blocks):
            if not isinstance(block_obj, dict):
                continue
            block = cast("dict[str, Any]", block_obj)
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str):
                parts.append(text)
        return "".join(parts)


def create_model_client(
    settings: ModelSettings, transport: httpx.AsyncBaseTransport | None = None
) -> ModelClient:
    """Factory: OpenAI-compatible vs Anthropic, mirroring kb_agent's provider branch."""
    if settings.provider in _OPENAI_COMPATIBLE:
        return OpenAICompatModelClient(settings, transport)
    return AnthropicModelClient(settings, transport)
