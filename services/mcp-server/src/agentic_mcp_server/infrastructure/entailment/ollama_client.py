"""Ollama-first EntailmentClient over any OpenAI-compatible chat endpoint (PR-31).

mcp-server's own copy of the provider-agnostic chat pattern kb-builder uses for
extraction and judging (services never share code). It defaults to a local
**Ollama** server (no cloud, no spend; `gemma3:4b` for L3 dev/tests) and repoints
to **Groq**, **OpenAI**, or **Azure OpenAI** by environment variables alone. The
`openai` SDK is confined to this module; the rest of the broker depends only on the
``EntailmentClient`` Protocol, so the backend stays swappable and the
import-boundary test still forbids the azure-search SDK here.

Configure via env:
- `ENTAIL_LLM_PROVIDER`: `ollama` (default) | `groq` | `openai` | `azure`
- OpenAI-compatible: `ENTAIL_LLM_BASE_URL`, `ENTAIL_LLM_API_KEY`, `ENTAIL_LLM_MODEL`
- Azure: `ENTAIL_AZURE_OPENAI_ENDPOINT`, `ENTAIL_AZURE_OPENAI_API_KEY`,
  `ENTAIL_AZURE_OPENAI_DEPLOYMENT`, `ENTAIL_AZURE_OPENAI_API_VERSION`

The entailment PROMPT is versioned by ``ENTAILMENT_PROMPT_VERSION`` so a prompt
change re-keys the cache (a new key ⇒ a miss ⇒ a fresh entailment).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import cast

from json_repair import repair_json
from openai import AsyncAzureOpenAI, AsyncOpenAI, BadRequestError
from openai.types.chat import ChatCompletionMessageParam

from agentic_mcp_server.infrastructure.entailment.client import EntailmentVerdict
from agentic_mcp_server.structured_logging import get_logger

logger = get_logger(__name__)

#: Bump on any prompt change so the entailment cache re-keys (CLAUDE.md invariant 4).
ENTAILMENT_PROMPT_VERSION = "entail-v1"

# Small local models occasionally emit malformed JSON; resample a few times.
_MAX_PARSE_ATTEMPTS = 3

# provider -> (default base_url, default api_key, default model)
_PROVIDER_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "ollama": ("http://localhost:11434/v1", "ollama", "gemma3:4b"),
    "groq": ("https://api.groq.com/openai/v1", "", "llama-3.1-8b-instant"),
    "openai": ("https://api.openai.com/v1", "", "gpt-4o-mini"),
}

_SYSTEM_PROMPT = (
    "You are a strict ENTAILMENT judge. You are given a CLAIM and one or more "
    "EVIDENCE passages. Decide ONLY whether the evidence, taken together, ENTAILS "
    "the claim — i.e. the claim is fully supported by what the evidence states. "
    "Return STRICT JSON only — no prose, no markdown fences. Use exactly this "
    'schema:\n{"entailed": true|false, "reason": "<one short sentence>"}\n'
    "RULES:\n"
    "- entailed = true ONLY if the evidence directly supports the claim. If the "
    "evidence is silent, contradicts, or only partially supports the claim, "
    "entailed = false.\n"
    "- Judge ONLY the given evidence; never use outside knowledge.\n"
    "- reason MUST be one short sentence and MUST NOT quote large spans of text."
)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for this entailment provider but is unset")
    return value


def _user_prompt(*, claim_text: str, evidence_texts: list[str]) -> str:
    evidence_block = "\n\n".join(
        f"EVIDENCE {index + 1}:\n{text}" for index, text in enumerate(evidence_texts)
    )
    return f"CLAIM:\n{claim_text}\n\n{evidence_block}\n\nReturn the JSON now."


def _extract_json(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _parse_verdict(raw: str) -> EntailmentVerdict:
    extracted = _extract_json(raw)
    try:
        data: object = json.loads(extracted)
    except json.JSONDecodeError:
        # Small local models truncate/degenerate; salvage what is recoverable.
        data = repair_json(extracted, return_objects=True)
        logger.warning("event=entailment_model_json_repaired")
    if not isinstance(data, dict) or not data:
        raise ValueError("entailment model did not return usable JSON")
    obj = cast("dict[str, object]", data)
    entailed = obj.get("entailed")
    reason = obj.get("reason")
    # A missing/non-bool verdict fails CLOSED (non-entailed): the verifier must
    # never treat an unparseable answer as support for a claim.
    if not isinstance(entailed, bool):
        logger.warning("event=entailment_bad_verdict")
        entailed = False
        reason = reason if isinstance(reason, str) else "unparseable verdict (failed closed)"
    return EntailmentVerdict(
        entailed=entailed,
        reason=reason if isinstance(reason, str) else "",
    )


class OllamaEntailmentClient:
    """EntailmentClient over an OpenAI-compatible (or Azure OpenAI) chat endpoint."""

    def __init__(
        self,
        client: AsyncOpenAI | AsyncAzureOpenAI,
        *,
        model: str,
        provider: str,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._model_version = hashlib.sha256(
            f"{provider}:{model}|temp={temperature}|prompt={ENTAILMENT_PROMPT_VERSION}".encode()
        ).hexdigest()[:16]

    @property
    def model_version(self) -> str:
        return self._model_version

    @classmethod
    def from_env(cls) -> OllamaEntailmentClient:
        provider = os.environ.get("ENTAIL_LLM_PROVIDER", "ollama").lower()
        temperature = float(os.environ.get("ENTAIL_LLM_TEMPERATURE", "0"))
        max_tokens = int(os.environ.get("ENTAIL_LLM_MAX_TOKENS", "512"))
        client: AsyncOpenAI | AsyncAzureOpenAI
        if provider == "azure":
            client = AsyncAzureOpenAI(
                azure_endpoint=_require_env("ENTAIL_AZURE_OPENAI_ENDPOINT"),
                api_key=_require_env("ENTAIL_AZURE_OPENAI_API_KEY"),
                api_version=os.environ.get("ENTAIL_AZURE_OPENAI_API_VERSION", "2024-06-01"),
            )
            model = _require_env("ENTAIL_AZURE_OPENAI_DEPLOYMENT")
        else:
            base_url, default_key, default_model = _PROVIDER_DEFAULTS.get(
                provider, _PROVIDER_DEFAULTS["ollama"]
            )
            api_key = os.environ.get("ENTAIL_LLM_API_KEY", default_key)
            if not api_key:
                raise RuntimeError(f"ENTAIL_LLM_API_KEY is required for provider {provider!r}")
            client = AsyncOpenAI(
                base_url=os.environ.get("ENTAIL_LLM_BASE_URL", base_url), api_key=api_key
            )
            model = os.environ.get("ENTAIL_LLM_MODEL", default_model)
        logger.info("event=entailment_client_configured provider=%s model=%s", provider, model)
        return cls(
            client, model=model, provider=provider, temperature=temperature, max_tokens=max_tokens
        )

    async def check_entailment(
        self, *, claim_text: str, evidence_texts: list[str]
    ) -> EntailmentVerdict:
        user_content = _user_prompt(claim_text=claim_text, evidence_texts=evidence_texts)
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        last_error: ValueError | None = None
        for attempt in range(_MAX_PARSE_ATTEMPTS):
            raw = await self._complete(messages)
            try:
                return _parse_verdict(raw)
            except ValueError as error:
                last_error = error
                logger.warning(
                    "event=entailment_parse_retry attempt=%d/%d", attempt + 1, _MAX_PARSE_ATTEMPTS
                )
        # All resamples failed to parse: fail CLOSED (non-entailed), never raise —
        # an unparseable model answer must never block a receipt or fabricate support.
        logger.warning("event=entailment_parse_exhausted failing_closed=true")
        assert last_error is not None
        return EntailmentVerdict(entailed=False, reason="entailment model output unparseable")

    async def _complete(self, messages: list[ChatCompletionMessageParam]) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                response_format={"type": "json_object"},
                messages=messages,
            )
        except BadRequestError:
            # some local/older endpoints reject response_format; the parser tolerates
            # fenced/prose output, so retry without forcing JSON mode.
            logger.warning("event=entailment_json_mode_unsupported model=%s", self._model)
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                messages=messages,
            )
        return response.choices[0].message.content or ""


__all__ = ["ENTAILMENT_PROMPT_VERSION", "OllamaEntailmentClient"]
