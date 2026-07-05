"""Bounded provider-400 retry in `_model_step` (evaluation-system.md §2, "adopted"
runtime retry against a machine-checkable validator).

Baseline this targets: docs/reports/evaluation-2026-07-05.md measured 3-6 flaked
arms per 20 in the live T3 A/B (a hallucinated tool name -> provider 400 -> the arm
ends early). These tests pin the retry's shape directly against a scripted fake
provider client, with no LLM credentials and no network: recovery (one 400, then a
good response) costs exactly one extra call; two consecutive 400s propagate exactly
as before (bounded, never "until it passes"); and a non-400 failure (e.g. a rate
limit) is never retried by this mechanism at all.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kb_agent


class _Provider400(Exception):
    """Stands in for openai.BadRequestError / anthropic.BadRequestError: both SDKs
    set `.status_code` on the raised exception (see their APIStatusError base)."""

    status_code = 400


class _RateLimited(Exception):
    """A DIFFERENT provider failure shape -- must never trigger this retry."""

    status_code = 429


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls: list[Any] = []

    def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
        return {"role": "assistant", "content": self.content}


class _FakeResponse:
    def __init__(self, content: str, prompt_tokens: int = 10, completion_tokens: int = 5) -> None:
        self.choices = [SimpleNamespace(message=_FakeMessage(content))]
        self.usage = SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )


class _ScriptedOpenAIClient:
    """`.chat.completions.create` raises or returns per a scripted sequence, one
    entry consumed per call (the last entry repeats if over-called)."""

    def __init__(self, script: list[Exception | _FakeResponse]) -> None:
        self._script = list(script)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **_kwargs: Any) -> _FakeResponse:
        outcome = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


_TOOLS = [{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}]


def _messages() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "hi"}]


def test_recovers_after_one_provider_400_with_exactly_one_retry() -> None:
    client = _ScriptedOpenAIClient(
        [_Provider400("invalid tool name 'serach_code'"), _FakeResponse("done")]
    )
    messages = _messages()
    _native, text, _tool_uses, _di, _do, retried = kb_agent._model_step(
        client, "openai", "m", "sys", _TOOLS, messages
    )
    assert retried is True
    assert text == "done"
    assert client.calls == 2  # exactly one extra call, not open-ended retrying


def test_retry_nudge_is_fed_back_verbatim_and_names_the_valid_tools() -> None:
    client = _ScriptedOpenAIClient(
        [_Provider400("invalid tool name 'serach_code'"), _FakeResponse("done")]
    )
    messages = _messages()
    kb_agent._model_step(client, "openai", "m", "sys", _TOOLS, messages)
    # _model_step does not append the assistant reply itself (the caller does, per its
    # docstring) -- so after the call, the nudge is the newest message.
    nudge = messages[-1]
    assert nudge["role"] == "user"
    assert "invalid tool name 'serach_code'" in nudge["content"]  # verbatim, not paraphrased
    assert "read_file" in nudge["content"]  # the available tools are named


def test_two_consecutive_400s_propagate_exactly_as_before() -> None:
    client = _ScriptedOpenAIClient([_Provider400("bad-1"), _Provider400("bad-2")])
    messages = _messages()
    with pytest.raises(_Provider400, match="bad-2"):
        kb_agent._model_step(client, "openai", "m", "sys", _TOOLS, messages)
    assert client.calls == 2  # bounded: the exhausted retry does not retry again


def test_non_400_errors_are_never_retried_by_this_mechanism() -> None:
    client = _ScriptedOpenAIClient([_RateLimited("slow down"), _FakeResponse("done")])
    messages = _messages()
    with pytest.raises(_RateLimited):
        kb_agent._model_step(client, "openai", "m", "sys", _TOOLS, messages)
    assert client.calls == 1  # a rate limit is not the adopted failure shape


def test_first_attempt_success_reports_no_retry() -> None:
    client = _ScriptedOpenAIClient([_FakeResponse("first try")])
    messages = _messages()
    _native, text, _tool_uses, _di, _do, retried = kb_agent._model_step(
        client, "openai", "m", "sys", _TOOLS, messages
    )
    assert retried is False
    assert text == "first try"
    assert client.calls == 1
