"""Hermetic tests for the shared model-endpoint resolver (no LLM, no DB, no network).

Covers the ``anthropic_foundry`` provider branch: Claude on Azure AI Foundry reuses the generic
LLM_* vars (LLM_BASE_URL is the .../anthropic endpoint, LLM_MODEL is the deployment) and fails
loudly when any required credential is missing — work is never silently dropped.
"""

import pytest

from agentic_kb_builder.infrastructure.azure_openai.llm_endpoint import (
    ANTHROPIC_FOUNDRY_PROVIDER,
    resolve_endpoint_from_env,
)

_LLM_VARS = (
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_MAX_TOKENS",
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
