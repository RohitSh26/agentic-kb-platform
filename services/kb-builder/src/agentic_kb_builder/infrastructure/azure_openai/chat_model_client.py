"""Concrete ModelClient over any OpenAI-compatible chat endpoint.

Lets wikify generation be validated locally and cheaply: it defaults to a local
**Ollama** server (no cloud, no spend) and repoints to **Groq**, **OpenAI**, or
**Azure OpenAI** by environment variables alone. The `openai` SDK is confined to
this module — the rest of the build plane depends only on the `ModelClient`
protocol (rule python.md), so the model backend stays swappable.

Configure via env (see the wiki "Run locally"):
- `LLM_PROVIDER`: `ollama` (default) | `groq` | `openai` | `azure`
- OpenAI-compatible providers: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
- Azure: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`,
  `AZURE_OPENAI_API_VERSION`
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from typing import cast

from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError

from agentic_kb_builder.domain import Chunk, WikifyGeneration
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# provider -> (default base_url, default api_key, default model)
_PROVIDER_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "ollama": ("http://localhost:11434/v1", "ollama", "llama3.1"),
    "groq": ("https://api.groq.com/openai/v1", "", "llama-3.1-8b-instant"),
    "openai": ("https://api.openai.com/v1", "", "gpt-4o-mini"),
}

_SYSTEM_PROMPT = (
    "You analyze ONE source document and return STRICT JSON only — no prose, no "
    "markdown fences. Use exactly this schema:\n"
    '{"summary": "<2-4 sentence neutral summary>",\n'
    ' "concepts": [{"name": "<short noun phrase>", "description": "<1-2 sentences>"}],\n'
    ' "facts": [{"statement": "<a specific claim>", "quote": "<verbatim span copied '
    'exactly from the source that supports the statement>"}]}\n'
    "Every fact.quote MUST be an exact substring of the source text, copied "
    "character-for-character — facts whose quote is not found verbatim in the source "
    "are discarded downstream. Prefer 3-7 concepts and 2-6 facts."
)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for this LLM provider but is unset")
    return value


def _user_prompt(chunks: Sequence[Chunk]) -> str:
    body = "\n\n".join(chunk.text for chunk in chunks)
    return f"SOURCE DOCUMENT:\n{body}\n\nReturn the JSON now."


def _extract_json(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _clean_items(items: object, keys: tuple[str, ...]) -> list[dict[str, str]]:
    """Keep only well-formed objects with non-empty string values for `keys`.

    Small local models are sloppy — drop malformed entries rather than failing the
    whole generation (and never invent: missing fields are simply not kept).
    """
    cleaned: list[dict[str, str]] = []
    if not isinstance(items, list):
        return cleaned
    for item in cast("list[object]", items):
        if not isinstance(item, dict):
            continue
        obj = cast("dict[str, object]", item)
        values = {key: obj.get(key) for key in keys}
        if all(isinstance(value, str) and value.strip() for value in values.values()):
            cleaned.append({key: str(values[key]) for key in keys})
    return cleaned


def _parse_generation(raw: str) -> WikifyGeneration:
    try:
        data: object = json.loads(_extract_json(raw))
    except json.JSONDecodeError as error:
        logger.error("event=wikify_model_bad_json error=%s", error)
        raise ValueError(f"model did not return valid JSON: {raw[:200]!r}") from error
    if not isinstance(data, dict):
        raise ValueError("model output JSON was not an object")
    obj = cast("dict[str, object]", data)
    summary = obj.get("summary")
    payload = {
        "summary": summary if isinstance(summary, str) else "",
        "concepts": _clean_items(obj.get("concepts"), ("name", "description")),
        "facts": _clean_items(obj.get("facts"), ("statement", "quote")),
    }
    try:
        return WikifyGeneration.model_validate(payload)
    except ValidationError as error:
        logger.error("event=wikify_model_bad_shape error=%s", error)
        raise ValueError(f"model JSON did not match the wikify schema: {error}") from error


class ChatModelClient:
    """ModelClient over an OpenAI-compatible (or Azure OpenAI) chat endpoint."""

    def __init__(
        self,
        client: AsyncOpenAI | AsyncAzureOpenAI,
        *,
        model: str,
        provider: str,
        temperature: float = 0.0,
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self.model_name = f"{provider}:{model}"
        self.model_params_hash = hashlib.sha256(
            f"{self.model_name}|temp={temperature}".encode()
        ).hexdigest()[:16]

    @classmethod
    def from_env(cls) -> ChatModelClient:
        provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
        temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
        client: AsyncOpenAI | AsyncAzureOpenAI
        if provider == "azure":
            client = AsyncAzureOpenAI(
                azure_endpoint=_require_env("AZURE_OPENAI_ENDPOINT"),
                api_key=_require_env("AZURE_OPENAI_API_KEY"),
                api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-06-01"),
            )
            model = _require_env("AZURE_OPENAI_DEPLOYMENT")
        else:
            base_url, default_key, default_model = _PROVIDER_DEFAULTS.get(
                provider, _PROVIDER_DEFAULTS["ollama"]
            )
            api_key = os.environ.get("LLM_API_KEY", default_key)
            if not api_key:
                raise RuntimeError(f"LLM_API_KEY is required for provider {provider!r}")
            client = AsyncOpenAI(base_url=os.environ.get("LLM_BASE_URL", base_url), api_key=api_key)
            model = os.environ.get("LLM_MODEL", default_model)
        logger.info("event=model_client_configured provider=%s model=%s", provider, model)
        return cls(client, model=model, provider=provider, temperature=temperature)

    async def generate_wikify(
        self, *, chunks: Sequence[Chunk], prompt_version: str
    ) -> WikifyGeneration:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(chunks)},
        ]
        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            response_format={"type": "json_object"},
            messages=messages,
        )
        raw = response.choices[0].message.content or ""
        return _parse_generation(raw)
