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


# --- Anthropic Foundry (Messages API) dispatch ------------------------------------------------
# A DIFFERENT SDK/API: the system prompt is a top-level system= param, the reply is a LIST of
# content blocks, and usage is input_tokens/output_tokens. These stubs mimic that shape with NO
# network so the _call_anthropic translation is exercised hermetically.


class _StubBlock:
    def __init__(self, *, type: str, text: str) -> None:
        self.type = type
        self.text = text


class _StubAnthropicUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _StubAnthropicMessage:
    def __init__(self, text: str, usage: _StubAnthropicUsage) -> None:
        self.content = [_StubBlock(type="text", text=text)]
        self.usage = usage


class _StubMessages:
    """Records the create() kwargs so the test can assert system= routing, returns a canned msg."""

    def __init__(self, message: _StubAnthropicMessage) -> None:
        self._message = message
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _StubAnthropicMessage:
        self.calls.append(kwargs)
        return self._message


class _StubAnthropicFoundry:
    """Minimal stand-in for AsyncAnthropicFoundry — no network, canned Messages-API response."""

    def __init__(self, text: str, usage: _StubAnthropicUsage) -> None:
        self.messages = _StubMessages(_StubAnthropicMessage(text, usage))


async def test_anthropic_completes_via_system_param_and_joins_text_blocks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub = _StubAnthropicFoundry(
        _GOOD_JUDGMENT, _StubAnthropicUsage(input_tokens=88, output_tokens=22)
    )
    client = ChatModelClient(
        stub,  # type: ignore[arg-type]
        model="claude-sonnet-4-6",
        provider="anthropic_foundry",
        is_anthropic=True,
    )
    with caplog.at_level(logging.INFO):
        judgment = await client.generate_relationship_judgment(
            candidate=_CANDIDATE, prompt_version="v1"
        )
    # The judge JSON parsing path is unchanged: a real verdict came back from the joined text.
    assert judgment.relation_type == "documents"

    # The system prompt was passed via system=, NOT as a role=system message.
    call = stub.messages.calls[0]
    assert isinstance(call["system"], str) and call["system"].strip()
    assert all(m["role"] != "system" for m in call["messages"])
    assert call["messages"][0]["role"] == "user"
    assert "response_format" not in call  # no JSON-mode param on the Messages API
    assert call["model"] == "claude-sonnet-4-6"

    # Usage is metered from input_tokens/output_tokens on the single model_call log line.
    line = next(r.getMessage() for r in caplog.records if "event=model_call " in r.getMessage())
    assert "model=anthropic_foundry:claude-sonnet-4-6" in line
    assert "prompt_tokens=88" in line
    assert "completion_tokens=22" in line
    assert "total_tokens=110" in line


async def test_anthropic_complete_returns_joined_text_blocks() -> None:
    stub = _StubAnthropicFoundry(
        "hello world", _StubAnthropicUsage(input_tokens=1, output_tokens=1)
    )
    client = ChatModelClient(
        stub,  # type: ignore[arg-type]
        model="claude-sonnet-4-6",
        provider="anthropic_foundry",
        is_anthropic=True,
    )
    text = await client._complete(
        [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "U"},
        ],
        purpose="judge",
    )
    assert text == "hello world"
    assert stub.messages.calls[0]["system"] == "SYS"
