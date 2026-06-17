"""Hermetic tests for docify endpoint resolution (ADR-0023). No graphify, no LLM, no DB.

Proves LLM_PROVIDER routing — including the native Azure OpenAI branch — and that a
missing key/deployment fails loudly (docs are never silently dropped).
"""

import pytest

from agentic_kb_builder.docify.extract_fn import resolve_endpoint

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


def test_azure_provider_reads_azure_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my-aoai.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini-deploy")
    provider, endpoint, api_key, model, max_tokens = resolve_endpoint()
    assert provider == "azure"
    assert endpoint == "https://my-aoai.openai.azure.com"
    assert api_key == "secret-key"
    assert model == "gpt-4o-mini-deploy"  # the deployment IS the model
    assert max_tokens == 8192


def test_azure_provider_missing_vars_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret-key")
    # endpoint + deployment missing
    with pytest.raises(RuntimeError, match=r"AZURE_OPENAI_ENDPOINT.*AZURE_OPENAI_DEPLOYMENT"):
        resolve_endpoint()


def test_openai_compatible_provider_uses_llm_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "gsk_test")
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
    provider, base_url, api_key, model, _ = resolve_endpoint()
    assert provider == "groq"
    assert base_url == "https://api.groq.com/openai/v1"  # provider default
    assert api_key == "gsk_test"
    assert model == "llama-3.3-70b-versatile"


def test_openai_compatible_missing_key_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")  # default key is empty
    with pytest.raises(RuntimeError, match="LLM_API_KEY is required"):
        resolve_endpoint()
