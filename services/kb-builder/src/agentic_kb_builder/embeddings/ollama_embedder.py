"""Real semantic embedder behind the Embedder Protocol (ADR-0019).

Calls an OpenAI-compatible / Ollama embeddings endpoint so the linker's
SimilarityProvider can find prose<->code matches by MEANING — the cross-domain
signal the LocalHashEmbedder (deterministic hash vector) cannot provide. Defaults
to local Ollama `nomic-embed-text` (768-dim), free and offline; point it at a
hosted endpoint by setting EMBEDDINGS_BASE_URL/-_MODEL/-_API_KEY with no code change.

Embedding a code span here is a deterministic vector lookup with NO generated
tokens, so it does not violate ADR-0018 (code is never summarised by a chat model).
The vector is cached in embedding_cache by content hash (invariant 4): unchanged
text is never re-embedded.
"""

import os

import httpx

from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.domain.embedding_port import EmbeddingResult
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "nomic-embed-text"
_TIMEOUT = 60.0


class OllamaEmbedder:
    """Embedder Protocol impl backed by an Ollama `/api/embeddings` endpoint.

    Holds one AsyncClient for the build's lifetime (call aclose() when done). The
    endpoint is local by default, so no auth header is sent unless EMBEDDINGS_API_KEY
    is set (for a hosted OpenAI-compatible gateway).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.embedding_model = model or os.environ.get("EMBEDDINGS_MODEL", _DEFAULT_MODEL)
        self._base_url = (
            base_url or os.environ.get("EMBEDDINGS_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        key = api_key if api_key is not None else os.environ.get("EMBEDDINGS_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        # Injected client in tests (MockTransport); a real pooled client otherwise.
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT, headers=headers)

    @classmethod
    def from_env(cls) -> "OllamaEmbedder":
        return cls()

    async def embed(self, text: str) -> EmbeddingResult:
        resp = await self._client.post(
            f"{self._base_url}/api/embeddings",
            json={"model": self.embedding_model, "prompt": text},
        )
        if resp.status_code >= 400:
            # No request body / token in the message — endpoint + status only.
            raise RuntimeError(
                f"embeddings endpoint {self._base_url} returned {resp.status_code} "
                f"for model {self.embedding_model}"
            )
        vector = resp.json().get("embedding")
        if not vector:
            raise RuntimeError(
                f"embeddings endpoint returned no vector for model {self.embedding_model}"
            )
        return EmbeddingResult(embedding_hash=content_hash(text), vector=[float(v) for v in vector])

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["OllamaEmbedder"]
