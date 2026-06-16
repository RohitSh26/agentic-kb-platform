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
import time
from collections.abc import Sequence
from typing import cast

from json_repair import repair_json
from openai import AsyncAzureOpenAI, AsyncOpenAI, BadRequestError
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError

from agentic_kb_builder.domain import (
    Chunk,
    JudgeCandidate,
    RelationshipJudgment,
    WikifyGeneration,
)
from agentic_kb_builder.domain.judge_records import (
    JUDGE_RELATION_TYPES,
    JUDGE_TRUST_BUCKETS,
    guard_quote,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Small local models occasionally emit malformed JSON; resample a few times before failing.
_MAX_PARSE_ATTEMPTS = 3

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


_JUDGE_SYSTEM_PROMPT = (
    "You judge whether TWO knowledge artifacts from different domains (e.g. a doc and "
    "a code file) are genuinely related. Return STRICT JSON only — no prose, no "
    "markdown fences. Use exactly this schema:\n"
    '{"relation_type": "documents",\n'
    ' "trust_bucket": "INFERRED_HIGH | INFERRED_LOW | AMBIGUOUS | REJECTED",\n'
    ' "supporting_quote": "<verbatim span copied character-for-character from one of '
    'the two source texts that supports the relationship>",\n'
    ' "reason": "<1-2 sentences>"}\n'
    "RULES:\n"
    "- relation_type MUST be exactly 'documents' (artifact A documents artifact B). "
    "Never invent any other relation; never output 'related_to'.\n"
    "- trust_bucket: INFERRED_HIGH = strong explicit evidence; INFERRED_LOW = weak or "
    "partial; AMBIGUOUS = cannot decide; REJECTED = not a real relationship.\n"
    "- supporting_quote MUST be an exact substring of one of the two source texts, "
    "copied character-for-character. A quote not found verbatim is rejected downstream "
    "and the judgment is downgraded to AMBIGUOUS.\n"
    "- You judge ONLY these two artifacts; never reference anything else."
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
    extracted = _extract_json(raw)
    try:
        data: object = json.loads(extracted)
    except json.JSONDecodeError:
        # Small local models truncate/degenerate (e.g. trailing tabs, an unclosed
        # object). Salvage what is structurally recoverable rather than discarding a
        # whole generation; _clean_items still drops any entry that survives malformed.
        data = repair_json(extracted, return_objects=True)
        logger.warning("event=wikify_model_json_repaired")
    if isinstance(data, list):
        # Some models wrap the single result object in a one-element array ([{...}]);
        # unwrap to the first object rather than failing an otherwise-valid generation.
        first = next(
            (
                cast("dict[str, object]", x)
                for x in cast("list[object]", data)
                if isinstance(x, dict)
            ),
            None,
        )
        if first is None:
            raise ValueError(f"model did not return usable JSON: {raw[:1000]!r}")
        data = first
        logger.warning("event=wikify_model_json_unwrapped_array")
    if not isinstance(data, dict) or not data:
        # Pure prose ("I cannot help...") repairs to nothing usable; fail loudly so the
        # caller's retry loop resamples rather than recording an empty generation.
        raise ValueError(f"model did not return usable JSON: {raw[:1000]!r}")
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


def _judge_user_prompt(candidate: JudgeCandidate) -> str:
    return (
        f"ARTIFACT A (title: {candidate.from_endpoint.title}):\n"
        f"{candidate.from_endpoint.evidence_text}\n\n"
        f"ARTIFACT B (title: {candidate.to_endpoint.title}):\n"
        f"{candidate.to_endpoint.evidence_text}\n\n"
        "Does A document B? Return the JSON now."
    )


def _parse_judgment(raw: str) -> RelationshipJudgment:
    extracted = _extract_json(raw)
    try:
        data: object = json.loads(extracted)
    except json.JSONDecodeError:
        data = repair_json(extracted, return_objects=True)
        logger.warning("event=judge_model_json_repaired")
    if not isinstance(data, dict) or not data:
        raise ValueError(f"judge did not return usable JSON: {raw[:1000]!r}")
    obj = cast("dict[str, object]", data)
    relation = obj.get("relation_type")
    bucket = obj.get("trust_bucket")
    quote = obj.get("supporting_quote")
    reason = obj.get("reason")
    # A relation outside the judge's allowed vocabulary (incl. the banned
    # `related_to` or an EXTRACTED-only deterministic relation) is NOT promoted:
    # the pair is recorded as AMBIGUOUS, never invented as a real edge.
    if not (isinstance(relation, str) and relation in JUDGE_RELATION_TYPES):
        logger.warning("event=judge_bad_relation_type value=%r", relation)
        relation = "documents"
        bucket = "AMBIGUOUS"
    # An EXTRACTED (or unknown) bucket from the judge is forced to AMBIGUOUS — the
    # LLM judge may NEVER assign EXTRACTED (trust-buckets.md).
    if not (isinstance(bucket, str) and bucket in JUDGE_TRUST_BUCKETS):
        logger.warning("event=judge_bad_trust_bucket value=%r", bucket)
        bucket = "AMBIGUOUS"
    payload = {
        "relation_type": relation,
        "trust_bucket": bucket,
        "supporting_quote": quote if isinstance(quote, str) else "",
        "reason": reason if isinstance(reason, str) else "",
    }
    try:
        return RelationshipJudgment.model_validate(payload)
    except ValidationError as error:
        logger.error("event=judge_model_bad_shape error=%s", error)
        raise ValueError(f"judge JSON did not match the judgment schema: {error}") from error


class ChatModelClient:
    """ModelClient over an OpenAI-compatible (or Azure OpenAI) chat endpoint."""

    def __init__(
        self,
        client: AsyncOpenAI | AsyncAzureOpenAI,
        *,
        model: str,
        provider: str,
        temperature: float = 0.0,
        max_tokens: int = 4000,
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        # Without an explicit cap, Ollama truncates output at its tiny default
        # num_predict and the wikify JSON comes back unterminated. 4000 fits the
        # whole structured response for the chunk sizes we send.
        self._max_tokens = max_tokens
        self.model_name = f"{provider}:{model}"
        self.model_params_hash = hashlib.sha256(
            f"{self.model_name}|temp={temperature}|max_tokens={max_tokens}".encode()
        ).hexdigest()[:16]

    @classmethod
    def from_env(cls) -> ChatModelClient:
        provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
        temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "4000"))
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
        return cls(
            client, model=model, provider=provider, temperature=temperature, max_tokens=max_tokens
        )

    async def generate_wikify(
        self, *, chunks: Sequence[Chunk], prompt_version: str
    ) -> WikifyGeneration:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(chunks)},
        ]
        # Small local models are non-deterministic even at temperature 0 and
        # occasionally emit malformed JSON; a fresh sample usually parses, so
        # retry a few times before failing the whole generation.
        last_error: ValueError | None = None
        for attempt in range(_MAX_PARSE_ATTEMPTS):
            raw = await self._complete(messages, purpose="wikify")
            try:
                return _parse_generation(raw)
            except ValueError as error:
                last_error = error
                logger.warning(
                    "event=wikify_parse_retry attempt=%d/%d error=%s",
                    attempt + 1,
                    _MAX_PARSE_ATTEMPTS,
                    error,
                )
        assert last_error is not None
        raise last_error

    async def generate_relationship_judgment(
        self, *, candidate: JudgeCandidate, prompt_version: str
    ) -> RelationshipJudgment:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": _judge_user_prompt(candidate)},
        ]
        last_error: ValueError | None = None
        for attempt in range(_MAX_PARSE_ATTEMPTS):
            raw = await self._complete(messages, purpose="judge")
            try:
                judgment = _parse_judgment(raw)
                # Quote-guard at the call boundary (invariant 7): a non-verbatim
                # supporting_quote downgrades the verdict to AMBIGUOUS so a
                # fabricated quote can never become an INFERRED edge.
                guarded = guard_quote(judgment, cited_spans=candidate.cited_spans)
                if guarded.trust_bucket != judgment.trust_bucket:
                    logger.warning(
                        "event=judge_quote_guard_downgrade from=%s to=%s relation=%s",
                        judgment.trust_bucket,
                        guarded.trust_bucket,
                        guarded.relation_type,
                    )
                return guarded
            except ValueError as error:
                last_error = error
                logger.warning(
                    "event=judge_parse_retry attempt=%d/%d error=%s",
                    attempt + 1,
                    _MAX_PARSE_ATTEMPTS,
                    error,
                )
        assert last_error is not None
        raise last_error

    async def _complete(
        self, messages: list[ChatCompletionMessageParam], *, purpose: str = "wikify"
    ) -> str:
        started = time.monotonic()
        try:
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
                # fenced/prose output, so retry without forcing JSON mode
                logger.warning("event=model_json_mode_unsupported model=%s", self._model)
                response = await self._client.chat.completions.create(
                    model=self._model,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    messages=messages,
                )
        except Exception as error:
            latency_ms = (time.monotonic() - started) * 1000
            # No silent failures on the model path: record the failed call (provider:model
            # + purpose + latency) before the retry loop / caller sees the exception.
            logger.warning(
                "event=model_call_failed model=%s purpose=%s latency_ms=%.0f error=%s",
                self.model_name,
                purpose,
                latency_ms,
                f"{type(error).__name__}: {error}",
            )
            raise
        latency_ms = (time.monotonic() - started) * 1000
        # `usage` is optional on the OpenAI-compatible response (some local/older
        # endpoints omit it); read it defensively and log -1 when absent so the call is
        # still visible. The token counts are emitted as additive fields on every model
        # call so a human can watch spend accrue line by line.
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        logger.info(
            "event=model_call model=%s purpose=%s prompt_tokens=%s completion_tokens=%s "
            "total_tokens=%s latency_ms=%.0f",
            self.model_name,
            purpose,
            prompt_tokens if prompt_tokens is not None else -1,
            completion_tokens if completion_tokens is not None else -1,
            total_tokens if total_tokens is not None else -1,
            latency_ms,
        )
        return response.choices[0].message.content or ""
