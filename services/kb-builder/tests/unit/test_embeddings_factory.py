"""EMBEDDINGS_PROVIDER is validated, not a pure on/off gate (task #39).

Unset ⇒ the semantic-linker pass stays off (None, no HTTP client ever built). Set to a
recognized value ⇒ the matching, correctly-shaped embedder is returned. Any other value
⇒ a loud RuntimeError at build start — never a silent no-op, never a wrong-shape call.
"""

import pytest

from agentic_kb_builder.embeddings.factory import (
    VALID_EMBEDDINGS_PROVIDERS,
    semantic_embedder_from_env,
)
from agentic_kb_builder.embeddings.ollama_embedder import OllamaEmbedder
from agentic_kb_builder.embeddings.openai_embedder import OpenAIEmbedder


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("EMBEDDINGS_PROVIDER", "EMBEDDINGS_API_KEY", "EMBEDDINGS_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_unset_provider_skips_the_pass() -> None:
    assert semantic_embedder_from_env() is None


def test_ollama_provider_selects_ollama_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "ollama")
    embedder = semantic_embedder_from_env()
    assert isinstance(embedder, OllamaEmbedder)


def test_openai_provider_selects_openai_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "k")
    embedder = semantic_embedder_from_env()
    assert isinstance(embedder, OpenAIEmbedder)


def test_openai_provider_without_key_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")
    with pytest.raises(RuntimeError, match="EMBEDDINGS_API_KEY is required"):
        semantic_embedder_from_env()


def test_provider_value_is_case_and_whitespace_tolerant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "  OLLAMA  ")
    assert isinstance(semantic_embedder_from_env(), OllamaEmbedder)


def test_unknown_provider_value_fails_loudly_never_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "azure")
    with pytest.raises(RuntimeError, match="EMBEDDINGS_PROVIDER='azure' is not supported"):
        semantic_embedder_from_env()


def test_accepted_provider_set_is_pinned() -> None:
    # Drift guard: adding/removing a provider must be a deliberate, reviewed change.
    assert VALID_EMBEDDINGS_PROVIDERS == ("ollama", "openai")
