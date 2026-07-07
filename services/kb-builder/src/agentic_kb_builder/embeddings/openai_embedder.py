"""OpenAI-compatible semantic embedder behind the Embedder Protocol (ADR-0019).

Calls the OpenAI `/v1/embeddings` request/response shape
(`{"model", "input"} -> {"data": [{"embedding": [...]}]}`) — the shape a real OpenAI
account, an Azure OpenAI embeddings deployment fronted by an OpenAI-compatible route,
or any other `/v1/embeddings`-speaking gateway expects. This is a DIFFERENT wire shape
from Ollama's native `/api/embeddings` (`OllamaEmbedder`); `EMBEDDINGS_PROVIDER`
selects between the two (`embeddings/factory.py`) so the two shapes are never crossed
silently. Selected via `EMBEDDINGS_PROVIDER=openai`; EMBEDDINGS_BASE_URL/-_MODEL/-_API_KEY
configure it exactly like `OllamaEmbedder`, except EMBEDDINGS_API_KEY is REQUIRED here —
unlike a local Ollama server, a real OpenAI-shaped endpoint always authenticates, so a
missing key fails loudly at construction rather than deep inside the first request.

Embedding a code span here is a deterministic vector lookup with NO generated tokens
(ADR-0018). The vector is cached in embedding_cache by content hash (invariant 4):
unchanged text is never re-embedded.
"""

import os

import httpx

from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.domain.embedding_port import EmbeddingResult
from agentic_kb_builder.embeddings.http_embedder import HttpEmbedder
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "text-embedding-3-small"


class OpenAIEmbedder(HttpEmbedder):
    """Embedder Protocol impl backed by an OpenAI-compatible `/v1/embeddings` endpoint."""

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
        if not key:
            raise RuntimeError("EMBEDDINGS_API_KEY is required when EMBEDDINGS_PROVIDER=openai")
        # Injected client in tests (MockTransport); a real pooled client otherwise.
        super().__init__(client=client, headers={"Authorization": f"Bearer {key}"})

    @classmethod
    def from_env(cls) -> "OpenAIEmbedder":
        return cls()

    async def embed(self, text: str) -> EmbeddingResult:
        resp = await self._client.post(
            f"{self._base_url}/embeddings",
            json={"model": self.embedding_model, "input": text},
        )
        if resp.status_code >= 400:
            # No request body / key in the message — endpoint + status only.
            raise RuntimeError(
                f"embeddings endpoint {self._base_url} returned {resp.status_code} "
                f"for model {self.embedding_model}"
            )
        data = resp.json().get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise RuntimeError(
                f"embeddings endpoint returned no data for model {self.embedding_model}"
            )
        vector = data[0].get("embedding")
        if not vector:
            raise RuntimeError(
                f"embeddings endpoint returned no vector for model {self.embedding_model}"
            )
        return EmbeddingResult(embedding_hash=content_hash(text), vector=[float(v) for v in vector])


__all__ = ["OpenAIEmbedder"]
