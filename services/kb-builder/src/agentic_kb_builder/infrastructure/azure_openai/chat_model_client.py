"""Concrete ModelClient over any OpenAI-compatible chat endpoint.

Backs the phase-3B relationship judge: it defaults to a local **Ollama** server (no
cloud, no spend) and repoints to **Groq**, **OpenAI**, or **Azure OpenAI** by
environment variables alone. The `openai` SDK is confined to this module — the rest of
the build plane depends only on the `ModelClient` protocol (rule python.md), so the
model backend stays swappable. The provider->endpoint resolution is shared with the
``docify`` adapter through the public ``llm_endpoint.resolve_endpoint_from_env`` (ADR-0023);
this module just builds the OpenAI/Azure SDK client from the resolved ``ModelEndpoint``.

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
from typing import cast

from json_repair import repair_json
from openai import AsyncAzureOpenAI, AsyncOpenAI, BadRequestError
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError

from agentic_kb_builder.domain import (
    JudgeCandidate,
    RelationshipJudgment,
)
from agentic_kb_builder.domain.judge_records import (
    JUDGE_RELATION_TYPES,
    JUDGE_TRUST_BUCKETS,
    guard_quote,
)
from agentic_kb_builder.infrastructure.azure_openai.llm_endpoint import resolve_endpoint_from_env
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Small local models occasionally emit malformed JSON; resample a few times before failing.
_MAX_PARSE_ATTEMPTS = 3

# Historical LLM_MAX_TOKENS fallback for the judge (docify uses a larger one) — kept so the
# generation-cache key / model identity stays stable.
_JUDGE_MAX_TOKENS_DEFAULT = 4000

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


def _extract_json(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _unwrap_list(data: object) -> object:
    """Unwrap a model response that came back as an array of objects to the first object.

    Some models (and json-repair on a multi-object / truncated payload) yield a list
    like [{...}, ...] instead of a single {...}; pick the first object so an otherwise
    usable generation is not discarded. Non-list input is returned unchanged.
    """
    if not isinstance(data, list):
        return data
    items = cast("list[object]", data)
    first = next((cast("dict[str, object]", x) for x in items if isinstance(x, dict)), None)
    if first is None:
        return items
    logger.warning("event=model_json_unwrapped_array")
    return first


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
    data = _unwrap_list(data)
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
        # num_predict and the JSON comes back unterminated. The default fits the whole
        # structured judge response.
        self._max_tokens = max_tokens
        self.model_name = f"{provider}:{model}"
        self.model_params_hash = hashlib.sha256(
            f"{self.model_name}|temp={temperature}|max_tokens={max_tokens}".encode()
        ).hexdigest()[:16]

    @classmethod
    def from_env(cls) -> ChatModelClient:
        # ONE shared provider resolution (no duplicated provider->endpoint map); temperature is
        # judge-specific so it stays here. The azure branch builds AsyncAzureOpenAI from the
        # resolved azure fields; every other provider builds AsyncOpenAI(base_url=..., api_key=...).
        endpoint = resolve_endpoint_from_env(max_tokens_default=_JUDGE_MAX_TOKENS_DEFAULT)
        temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
        client: AsyncOpenAI | AsyncAzureOpenAI
        if endpoint.is_azure:
            client = AsyncAzureOpenAI(
                azure_endpoint=endpoint.azure_endpoint,
                api_key=endpoint.api_key,
                api_version=endpoint.azure_api_version,
            )
        else:
            client = AsyncOpenAI(base_url=endpoint.base_url, api_key=endpoint.api_key)
        logger.info(
            "event=model_client_configured provider=%s model=%s",
            endpoint.provider,
            endpoint.model,
        )
        return cls(
            client,
            model=endpoint.model,
            provider=endpoint.provider,
            temperature=temperature,
            max_tokens=endpoint.max_tokens,
        )

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
        self, messages: list[ChatCompletionMessageParam], *, purpose: str = "judge"
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
