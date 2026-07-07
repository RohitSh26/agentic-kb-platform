"""ModelClient shim: provider factory + response parsing, all via MockTransport."""

import json

import httpx
import pytest

from review_panel.domain.errors import ModelAPIError
from review_panel.infrastructure.model_client import (
    _ANTHROPIC_LIKE,
    _OPENAI_COMPATIBLE,
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


# --- anthropic_foundry (task #38b: was missing despite the "mirrors kb_agent.py" docstring) -----


def test_settings_anthropic_foundry_requires_base_url_and_model() -> None:
    settings = load_model_settings(
        {
            "LLM_PROVIDER": "anthropic_foundry",
            "LLM_API_KEY": "k",
            "LLM_MODEL": "claude-sonnet-4-6",
            "LLM_BASE_URL": "https://my-resource.services.ai.azure.com/anthropic",
        }
    )
    assert settings.base_url == "https://my-resource.services.ai.azure.com/anthropic"
    with pytest.raises(ModelAPIError, match="LLM_BASE_URL is required"):
        load_model_settings(
            {"LLM_PROVIDER": "anthropic_foundry", "LLM_API_KEY": "k", "LLM_MODEL": "m"}
        )
    with pytest.raises(ModelAPIError, match="LLM_MODEL is required"):
        load_model_settings(
            {
                "LLM_PROVIDER": "anthropic_foundry",
                "LLM_API_KEY": "k",
                "LLM_BASE_URL": "https://x.services.ai.azure.com/anthropic",
            }
        )


def test_factory_dispatches_anthropic_foundry_to_the_anthropic_client() -> None:
    settings = _settings("anthropic_foundry", "https://my-resource.services.ai.azure.com/anthropic")
    assert isinstance(create_model_client(settings), AnthropicModelClient)


async def test_anthropic_foundry_sends_both_x_api_key_and_api_key_headers() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/anthropic/v1/messages"  # base_url already has /anthropic
        seen["x-api-key"] = request.headers.get("x-api-key")
        seen["api-key"] = request.headers.get("api-key")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    settings = _settings("anthropic_foundry", "https://my-resource.services.ai.azure.com/anthropic")
    client = AnthropicModelClient(settings, transport=httpx.MockTransport(handler))
    assert await client.complete(system="s", user="u") == "ok"
    assert seen["x-api-key"] == "k"
    assert seen["api-key"] == "k"  # Foundry-only backwards-compat header


async def test_native_anthropic_sends_only_x_api_key_not_api_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["api-key"] = request.headers.get("api-key")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = AnthropicModelClient(
        _settings("anthropic", "https://llm.test"), transport=httpx.MockTransport(handler)
    )
    await client.complete(system="s", user="u")
    assert seen["api-key"] is None


# --- azure is deliberately unsupported (matches kb_agent.py, which never had it) ----------------


def test_azure_provider_is_not_supported() -> None:
    with pytest.raises(ModelAPIError, match="unsupported LLM_PROVIDER: 'azure'"):
        load_model_settings({"LLM_PROVIDER": "azure", "LLM_API_KEY": "k", "LLM_MODEL": "m"})


# --- drift guard: the accepted LLM_PROVIDER set must match the module docstring -----------------


def test_accepted_provider_set_is_pinned() -> None:
    assert _OPENAI_COMPATIBLE == ("groq", "openai", "openai_compatible", "ollama")
    assert _ANTHROPIC_LIKE == ("anthropic", "anthropic_foundry")
