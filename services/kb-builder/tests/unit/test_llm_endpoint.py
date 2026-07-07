"""Hermetic tests for the shared model-endpoint resolver (no LLM, no DB, no network).

Covers the ``anthropic_foundry`` provider branch: Claude on Azure AI Foundry reuses the generic
LLM_* vars (LLM_BASE_URL is the .../anthropic endpoint, LLM_MODEL is the deployment) and fails
loudly when any required credential is missing — work is never silently dropped. Also covers the
``GROQ_API_KEY`` fallback (task #38) and pins the resolver's accepted-provider set as a drift
guard: the three LLM_PROVIDER consumers (kb-builder, ``scripts/kb_agent.py``, review-panel) must
not silently diverge again.
"""

import pytest

from agentic_kb_builder.infrastructure.azure_openai.llm_endpoint import (
    ANTHROPIC_FOUNDRY_PROVIDER,
    AZURE_PROVIDER,
    PROVIDER_DEFAULTS,
    resolve_endpoint_from_env,
)

_LLM_VARS = (
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_MAX_TOKENS",
    "GROQ_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_DEPLOYMENT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _LLM_VARS:
        monkeypatch.delenv(var, raising=False)


def test_anthropic_foundry_resolves_from_llm_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic_foundry")
    monkeypatch.setenv("LLM_BASE_URL", "https://my-resource.services.ai.azure.com/anthropic")
    monkeypatch.setenv("LLM_API_KEY", "secret-foundry-key")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    endpoint = resolve_endpoint_from_env(max_tokens_default=4000)
    assert endpoint.provider == ANTHROPIC_FOUNDRY_PROVIDER
    assert endpoint.is_anthropic_foundry
    assert not endpoint.is_azure
    assert endpoint.base_url == "https://my-resource.services.ai.azure.com/anthropic"
    assert endpoint.api_key == "secret-foundry-key"
    assert endpoint.model == "claude-sonnet-4-6"  # the Claude deployment name
    assert endpoint.max_tokens == 4000


@pytest.mark.parametrize("missing", ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"])
def test_anthropic_foundry_missing_var_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic_foundry")
    present = {
        "LLM_BASE_URL": "https://x.services.ai.azure.com/anthropic",
        "LLM_API_KEY": "k",
        "LLM_MODEL": "claude-sonnet-4-6",
    }
    for name, value in present.items():
        if name != missing:
            monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=rf"anthropic_foundry.*{missing}"):
        resolve_endpoint_from_env(max_tokens_default=4000)


# --- GROQ_API_KEY fallback (task #38a) ---------------------------------------------------------
# kb-builder used to read ONLY {env_prefix}_API_KEY, diverging from scripts/kb_agent.py and
# review-panel's ModelClient shim (both of which fall back to GROQ_API_KEY). All three now
# resolve a key the same way.


def test_generic_provider_falls_back_to_groq_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fallback")
    endpoint = resolve_endpoint_from_env(max_tokens_default=4000)
    assert endpoint.api_key == "gsk_fallback"


def test_generic_provider_prefers_llm_api_key_over_groq_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "explicit-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fallback")
    endpoint = resolve_endpoint_from_env(max_tokens_default=4000)
    assert endpoint.api_key == "explicit-key"


def test_generic_provider_without_any_key_still_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(RuntimeError, match="LLM_API_KEY is required for provider 'openai'"):
        resolve_endpoint_from_env(max_tokens_default=4000)


def test_ollama_still_needs_no_key_even_with_groq_key_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    endpoint = resolve_endpoint_from_env(max_tokens_default=4000)
    assert endpoint.api_key == "ollama"  # the provider's own dummy default, unauthenticated


# --- Drift guard: the resolver's accepted provider set ------------------------------------------


def test_accepted_provider_set_is_pinned() -> None:
    # Any addition/removal of a specially-handled provider (or a generic-provider default) is a
    # deliberate, reviewed change — not a silent divergence from the other two LLM_PROVIDER
    # consumers documented in docs/dev-guide/how-to/switch-llm-providers.md.
    assert AZURE_PROVIDER == "azure"
    assert ANTHROPIC_FOUNDRY_PROVIDER == "anthropic_foundry"
    assert set(PROVIDER_DEFAULTS) == {"ollama", "groq", "openai"}
