"""Hermetic test for the Anthropic-native docify path (Claude on Azure AI Foundry).

No network, no live LLM: a fake ``AsyncAnthropicFoundry`` returns a canned Messages-API
response. Proves the extractor returns the node-dict shape ``map_doc_extraction`` consumes,
forces ``source_file`` to the doc path, sends the system prompt + an <untrusted_source>-wrapped
document, and that ``make_doc_extract`` routes anthropic_foundry here (Graphify elsewhere).
"""

import json

import anthropic
import pytest

from agentic_kb_builder.docify import extract_fn
from agentic_kb_builder.docify.extract_fn import (
    _make_anthropic_foundry_doc_extract,
    make_doc_extract,
)
from agentic_kb_builder.infrastructure.azure_openai.llm_endpoint import ModelEndpoint

_ENDPOINT = ModelEndpoint(
    provider="anthropic_foundry",
    base_url="https://x.services.ai.azure.com/anthropic",
    api_key="k",
    model="claude-sonnet-4-6",
    max_tokens=8192,
)


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 11
    output_tokens = 22


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage()


class _Messages:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict] = []

    async def create(self, **kwargs: object) -> _Resp:
        self.calls.append(kwargs)
        return _Resp(self._text)


class _FakeFoundry:
    last: "_FakeFoundry | None" = None

    def __init__(self, **_kwargs: object) -> None:
        self.messages = _Messages(
            '{"nodes":[{"id":"c1","label":"login flow","file_type":"concept",'
            '"source_file":"WRONG.md","source_location":"validates a session token"}]}'
        )
        _FakeFoundry.last = self


async def test_anthropic_doc_extract_shape_and_source_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic, "AsyncAnthropicFoundry", _FakeFoundry)
    fn = _make_anthropic_foundry_doc_extract(_ENDPOINT)
    data = await fn(text="The login flow validates a session token.", doc_path="docs/auth.md")

    # node-dict shape map_doc_extraction consumes; source_file FORCED to the doc path
    (node,) = data["nodes"]
    assert node["file_type"] == "concept"
    assert node["source_file"] == "docs/auth.md"  # overridden from "WRONG.md"
    assert node["source_location"] == "validates a session token"

    # the system prompt is sent and the document is wrapped as untrusted data
    assert _FakeFoundry.last is not None
    (call,) = _FakeFoundry.last.messages.calls
    assert call["system"] is extract_fn._ANTHROPIC_DOC_EXTRACT_SYSTEM
    user = call["messages"][0]["content"]
    assert "<untrusted_source>" in user and "doc_path: docs/auth.md" in user
    assert call["model"] == "claude-sonnet-4-6"
    # never a response_format / JSON-mode kwarg (Anthropic has none)
    assert "response_format" not in call


def test_make_doc_extract_routes_anthropic_vs_graphify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic, "AsyncAnthropicFoundry", _FakeFoundry)
    assert getattr(make_doc_extract(_ENDPOINT), "__name__", "") == "anthropic_doc_extract"
    openai_ep = ModelEndpoint(
        provider="groq", base_url="https://api.groq.com/openai/v1", api_key="k", model="x",
        max_tokens=8192,
    )
    assert getattr(make_doc_extract(openai_ep), "__name__", "") == "graphify_doc_extract"


def test_malformed_json_is_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    # repair_json tolerates a trailing comma / missing brace, so a slightly-off model reply
    # still yields nodes rather than crashing the build.
    class _Loose(_FakeFoundry):
        def __init__(self, **_kwargs: object) -> None:
            self.messages = _Messages('{"nodes":[{"id":"c1","label":"x","file_type":"concept",}]')
            _Loose.last = self

    monkeypatch.setattr(anthropic, "AsyncAnthropicFoundry", _Loose)
    fn = _make_anthropic_foundry_doc_extract(_ENDPOINT)

    async def _run() -> None:
        data = await fn(text="t", doc_path="d.md")
        assert isinstance(data["nodes"], list)
        json.dumps(data)  # serialisable

    import asyncio

    asyncio.run(_run())
