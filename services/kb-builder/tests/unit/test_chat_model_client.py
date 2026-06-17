"""ChatModelClient model-call telemetry is robust + never silent (no network).

After ADR-0023 retired the wikify prose pipeline, the only build-plane LLM use behind this
door is the phase-3B relationship judge; these tests exercise the shared ``_complete`` path
(token-usage logging, usage-absent fallback, failure logging) via ``generate_relationship_
judgment`` against a stubbed OpenAI client. The judge JSON parser is covered in
test_judge_parsing.py.
"""

import logging
import uuid
from typing import Any

import pytest

from agentic_kb_builder.domain import JudgeCandidate, JudgeEndpoint
from agentic_kb_builder.infrastructure.azure_openai.chat_model_client import ChatModelClient


class _StubMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubChoice:
    def __init__(self, content: str) -> None:
        self.message = _StubMessage(content)


class _StubUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class _StubResponse:
    def __init__(self, content: str, usage: _StubUsage | None) -> None:
        self.choices = [_StubChoice(content)]
        self.usage = usage


class _StubCompletions:
    def __init__(self, response: _StubResponse) -> None:
        self._response = response

    async def create(self, **_: Any) -> _StubResponse:
        return self._response


class _StubChat:
    def __init__(self, response: _StubResponse) -> None:
        self.completions = _StubCompletions(response)


class _StubOpenAI:
    """A minimal stand-in for AsyncOpenAI — no network, returns a canned response."""

    def __init__(self, content: str, usage: _StubUsage | None) -> None:
        self.chat = _StubChat(_StubResponse(content, usage))


# A valid judge verdict whose supporting_quote is grounded in the candidate spans below.
_GOOD_JUDGMENT = (
    '{"relation_type": "documents", "trust_bucket": "INFERRED_HIGH",'
    ' "supporting_quote": "payment service", "reason": "doc names the service"}'
)

_CANDIDATE = JudgeCandidate(
    from_endpoint=JudgeEndpoint(uuid.uuid4(), "doc", "the payment service rollout", "h1"),
    to_endpoint=JudgeEndpoint(uuid.uuid4(), "code", "def charge(): ...", "h2"),
)


def _client(content: str, usage: _StubUsage | None) -> ChatModelClient:
    return ChatModelClient(
        _StubOpenAI(content, usage),  # type: ignore[arg-type]
        model="llama3.1",
        provider="ollama",
    )


async def test_model_call_logs_provider_model_purpose_and_token_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(_GOOD_JUDGMENT, _StubUsage(prompt=120, completion=45))
    with caplog.at_level(logging.INFO):
        await client.generate_relationship_judgment(candidate=_CANDIDATE, prompt_version="v1")
    line = next(r.getMessage() for r in caplog.records if "event=model_call " in r.getMessage())
    assert "model=ollama:llama3.1" in line
    assert "purpose=judge" in line
    assert "prompt_tokens=120" in line
    assert "completion_tokens=45" in line
    assert "total_tokens=165" in line
    assert "latency_ms=" in line


async def test_model_call_logs_when_usage_is_absent(caplog: pytest.LogCaptureFixture) -> None:
    # Some local/older endpoints omit `usage`; the call must still be visible (token
    # counts logged as -1) rather than crashing or going silent.
    client = _client(_GOOD_JUDGMENT, usage=None)
    with caplog.at_level(logging.INFO):
        await client.generate_relationship_judgment(candidate=_CANDIDATE, prompt_version="v1")
    line = next(r.getMessage() for r in caplog.records if "event=model_call " in r.getMessage())
    assert "prompt_tokens=-1" in line
    assert "total_tokens=-1" in line


async def test_model_call_failure_is_logged_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    class _Boom:
        async def create(self, **_: Any) -> object:
            raise RuntimeError("endpoint down")

    client = _client(_GOOD_JUDGMENT, usage=None)
    client._client.chat.completions = _Boom()  # type: ignore[attr-defined, assignment]
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="endpoint down"):
        await client.generate_relationship_judgment(candidate=_CANDIDATE, prompt_version="v1")
    assert any("event=model_call_failed" in r.getMessage() for r in caplog.records)
