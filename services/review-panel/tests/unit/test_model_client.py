"""ModelClient shim: provider factory + response parsing, all via MockTransport."""

import json

import httpx
import pytest

from review_panel.domain.errors import ModelAPIError
from review_panel.infrastructure.model_client import (
    AnthropicModelClient,
    ModelSettings,
    OpenAICompatModelClient,
    create_model_client,
    load_model_settings,
)


def _settings(provider: str = "groq", base_url: str = "https://llm.test/v1") -> ModelSettings:
    return ModelSettings(provider=provider, model="m1", api_key="k", base_url=base_url)


def test_factory_mirrors_kb_agent_provider_branch() -> None:
    for provider in ("groq", "openai", "openai_compatible", "ollama"):
        assert isinstance(create_model_client(_settings(provider)), OpenAICompatModelClient)
    assert isinstance(create_model_client(_settings("anthropic")), AnthropicModelClient)


def test_settings_default_base_urls_and_required_key() -> None:
    settings = load_model_settings({"LLM_PROVIDER": "groq", "GROQ_API_KEY": "k"})
    assert settings.base_url == "https://api.groq.com/openai/v1"
    assert settings.model == "llama-3.3-70b-versatile"
    with pytest.raises(ModelAPIError):
        load_model_settings({"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt"})  # no key
    with pytest.raises(ModelAPIError):
        load_model_settings({"LLM_PROVIDER": "nope"})
    # ollama needs no key
    local = load_model_settings({"LLM_PROVIDER": "ollama", "LLM_MODEL": "qwen3"})
    assert local.base_url == "http://localhost:11434/v1"


async def test_openai_compatible_parses_chat_completions() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    client = OpenAICompatModelClient(_settings(), transport=httpx.MockTransport(handler))
    out = await client.complete(system="sys", user="usr")
    assert out == '{"ok": true}'
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["messages"][0] == {"role": "system", "content": "sys"}


async def test_anthropic_parses_messages_content_blocks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        body = json.loads(request.content)
        assert body["system"] == "sys"
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        )

    client = AnthropicModelClient(
        _settings("anthropic", "https://llm.test"), transport=httpx.MockTransport(handler)
    )
    assert await client.complete(system="sys", user="usr") == "ab"


async def test_http_error_becomes_model_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = OpenAICompatModelClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelAPIError):
        await client.complete(system="s", user="u")
