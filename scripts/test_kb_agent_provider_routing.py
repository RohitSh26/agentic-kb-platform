"""LLM_PROVIDER dispatch drift guard for kb_agent.py (task #38).

Three consumers resolve LLM_PROVIDER (kb-builder's llm_endpoint.py, this script, and
review-panel's ModelClient shim); docs/dev-guide/how-to/switch-llm-providers.md documents
each one's exact accepted set. These tests pin kb_agent.py's actual dispatch (`_is_openai`
+ `_make_client`) against real SDK client construction (no network — SDK constructors are
local-only) so the next divergence from that doc fails loudly, not silently.
"""

import sys
from pathlib import Path

import pytest
from anthropic import Anthropic, AnthropicFoundry
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kb_agent

_ENV_VARS = ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "GROQ_API_KEY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_is_openai_accepts_exactly_groq_openai_openai_compatible() -> None:
    # Drift guard: this is the exact set the docstring/providers-page claim.
    assert kb_agent._is_openai("groq")
    assert kb_agent._is_openai("openai")
    assert kb_agent._is_openai("openai_compatible")
    assert not kb_agent._is_openai("anthropic")
    assert not kb_agent._is_openai("anthropic_foundry")
    assert not kb_agent._is_openai("azure")  # kb_agent.py has never supported azure


def test_make_client_dispatches_groq_to_the_openai_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "k")
    client, provider, _model = kb_agent._make_client()
    assert isinstance(client, OpenAI)
    assert provider == "groq"


def test_make_client_dispatches_openai_compatible_to_the_openai_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://custom.example/v1")
    client, provider, _model = kb_agent._make_client()
    assert isinstance(client, OpenAI)
    assert provider == "openai_compatible"


def test_make_client_falls_back_to_groq_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fallback")
    client, _provider, _model = kb_agent._make_client()
    assert isinstance(client, OpenAI)
    assert client.api_key == "gsk_fallback"


def test_make_client_dispatches_anthropic_foundry_to_the_foundry_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic_foundry")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://x.services.ai.azure.com/anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    client, provider, model = kb_agent._make_client()
    assert isinstance(client, AnthropicFoundry)
    assert provider == "anthropic_foundry"
    assert model == "claude-sonnet-4-6"


def test_make_client_dispatches_native_anthropic_for_the_anthropic_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    client, provider, _model = kb_agent._make_client()
    assert type(client) is Anthropic  # NOT AnthropicFoundry (which subclasses Anthropic)
    assert provider == "anthropic"


def test_make_client_treats_any_unrecognized_provider_as_native_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pinned CURRENT behavior, not an endorsement: kb_agent.py never validates
    # LLM_PROVIDER against a fixed set. Anything not in _is_openai() and not
    # "anthropic_foundry" falls through to the native Anthropic SDK path. If this ever
    # changes to a loud rejection instead, update this test deliberately.
    monkeypatch.setenv("LLM_PROVIDER", "some-typo'd-provider")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    client, provider, _model = kb_agent._make_client()
    assert type(client) is Anthropic
    assert provider == "some-typo'd-provider"
