"""ChatModelClient JSON parsing is robust to messy local-model output (no network).

The wikify model contract is strict JSON; small local models (Ollama) are sloppy, so
the parser strips fences/prose and drops malformed concepts/facts rather than failing
the whole generation or inventing fields.
"""

import logging
from typing import Any

import pytest

from agentic_kb_builder.domain import Chunk
from agentic_kb_builder.infrastructure.azure_openai.chat_model_client import (
    ChatModelClient,
    _parse_generation,
)


def test_parses_plain_json() -> None:
    raw = (
        '{"summary": "A doc about X.",'
        ' "concepts": [{"name": "X", "description": "the thing"}],'
        ' "facts": [{"statement": "X is Y", "quote": "X is Y"}]}'
    )
    generation = _parse_generation(raw)
    assert generation.summary == "A doc about X."
    assert generation.concepts[0].name == "X"
    assert generation.facts[0].quote == "X is Y"


def test_strips_markdown_fences_and_surrounding_prose() -> None:
    raw = 'Sure! Here is the JSON:\n```json\n{"summary": "S", "concepts": [], "facts": []}\n```'
    generation = _parse_generation(raw)
    assert generation.summary == "S"
    assert generation.concepts == ()
    assert generation.facts == ()


def test_drops_malformed_concepts_and_facts() -> None:
    raw = (
        '{"summary": "S",'
        ' "concepts": [{"name": "ok", "description": "d"}, {"name": "", "description": "x"}],'
        ' "facts": [{"statement": "s", "quote": ""}, "not-an-object"]}'
    )
    generation = _parse_generation(raw)
    assert len(generation.concepts) == 1
    assert generation.facts == ()


def test_non_json_output_raises_a_clear_error() -> None:
    # Pure prose has no JSON to salvage even after repair -> fail loudly (caller resamples).
    with pytest.raises(ValueError, match="did not return usable JSON"):
        _parse_generation("I cannot help with that.")


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


_GOOD_JSON = '{"summary": "S", "concepts": [], "facts": []}'


def _client(content: str, usage: _StubUsage | None) -> ChatModelClient:
    return ChatModelClient(
        _StubOpenAI(content, usage),  # type: ignore[arg-type]
        model="llama3.1",
        provider="ollama",
    )


async def test_model_call_logs_provider_model_purpose_and_token_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(_GOOD_JSON, _StubUsage(prompt=120, completion=45))
    with caplog.at_level(logging.INFO):
        await client.generate_wikify(
            chunks=[Chunk(index=0, text="hi", chunk_hash="h")], prompt_version="v1"
        )
    line = next(r.getMessage() for r in caplog.records if "event=model_call " in r.getMessage())
    assert "model=ollama:llama3.1" in line
    assert "purpose=wikify" in line
    assert "prompt_tokens=120" in line
    assert "completion_tokens=45" in line
    assert "total_tokens=165" in line
    assert "latency_ms=" in line


async def test_model_call_logs_when_usage_is_absent(caplog: pytest.LogCaptureFixture) -> None:
    # Some local/older endpoints omit `usage`; the call must still be visible (token
    # counts logged as -1) rather than crashing or going silent.
    client = _client(_GOOD_JSON, usage=None)
    with caplog.at_level(logging.INFO):
        await client.generate_wikify(
            chunks=[Chunk(index=0, text="hi", chunk_hash="h")], prompt_version="v1"
        )
    line = next(r.getMessage() for r in caplog.records if "event=model_call " in r.getMessage())
    assert "prompt_tokens=-1" in line
    assert "total_tokens=-1" in line


async def test_model_call_failure_is_logged_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    class _Boom:
        async def create(self, **_: Any) -> object:
            raise RuntimeError("endpoint down")

    client = _client(_GOOD_JSON, usage=None)
    client._client.chat.completions = _Boom()  # type: ignore[attr-defined, assignment]
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="endpoint down"):
        await client.generate_wikify(
            chunks=[Chunk(index=0, text="hi", chunk_hash="h")], prompt_version="v1"
        )
    assert any("event=model_call_failed" in r.getMessage() for r in caplog.records)


def test_repairs_truncated_local_model_json() -> None:
    # gemma3:4b-style degeneration: an unclosed object + trailing tabs. json-repair
    # salvages the well-formed prefix; malformed entries are still dropped by _clean_items.
    raw = (
        '{"summary": "A doc.", "concepts": [],'
        ' "facts": [{"statement": "s", "quote": "q"},'
        ' {"statement": "broken", "quote": "x\t\t\t'
    )
    generation = _parse_generation(raw)
    assert generation.summary == "A doc."
    assert generation.facts[0].quote == "q"
