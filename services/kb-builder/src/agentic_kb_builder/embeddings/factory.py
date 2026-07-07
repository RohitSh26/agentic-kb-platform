"""Selects the real (non-local) semantic embedder from EMBEDDINGS_PROVIDER (ADR-0019).

EMBEDDINGS_PROVIDER used to be a pure on/off gate — any non-empty value enabled the
pass and the value itself was never inspected, so it silently always spoke Ollama's
native `/api/embeddings` wire shape even if EMBEDDINGS_BASE_URL pointed at a real
OpenAI-shaped endpoint. It is now VALIDATED: `ollama` (default wire shape,
`OllamaEmbedder`) or `openai` (the `/v1/embeddings` shape, `OpenAIEmbedder`) are the
only accepted values. Any other value fails the build loudly at startup — never a
silent no-op and never a wrong-shape call to the wrong protocol.
"""

import os

from agentic_kb_builder.domain.embedding_port import Embedder
from agentic_kb_builder.embeddings.ollama_embedder import OllamaEmbedder
from agentic_kb_builder.embeddings.openai_embedder import OpenAIEmbedder
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_OLLAMA = "ollama"
_OPENAI = "openai"
VALID_EMBEDDINGS_PROVIDERS = (_OLLAMA, _OPENAI)


def semantic_embedder_from_env() -> Embedder | None:
    """None when EMBEDDINGS_PROVIDER is unset (the semantic linker pass is skipped,
    ADR-0019 — the default build stays offline + deterministic). Raises RuntimeError
    for any set-but-unrecognized value."""
    raw = os.environ.get("EMBEDDINGS_PROVIDER")
    if not raw:
        return None
    provider = raw.strip().lower()
    if provider == _OLLAMA:
        logger.info("event=embeddings_provider_selected provider=%s", provider)
        return OllamaEmbedder.from_env()
    if provider == _OPENAI:
        logger.info("event=embeddings_provider_selected provider=%s", provider)
        return OpenAIEmbedder.from_env()
    raise RuntimeError(
        f"EMBEDDINGS_PROVIDER={raw!r} is not supported; use one of "
        f"{', '.join(VALID_EMBEDDINGS_PROVIDERS)}"
    )


__all__ = ["VALID_EMBEDDINGS_PROVIDERS", "semantic_embedder_from_env"]
