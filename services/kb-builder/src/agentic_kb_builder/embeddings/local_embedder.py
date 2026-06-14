"""Local deterministic embedder: populate embedding_cache with no cloud, no spend.

The Search projection only needs *a* vector recorded for each indexable artifact so the
index stays rebuildable from Postgres (invariant 1/4). For the local development loop we
derive a small, fully deterministic vector from the content hash — same text always yields
the same vector, on any machine. Real semantic vectors come from the Azure/Ollama embedder
in production; this keeps the local build cost-free and reproducible.
"""

from agentic_kb_builder.application.build_runner import EmbeddingResult
from agentic_kb_builder.domain.content_hasher import content_hash

_DIMS = 8


class LocalHashEmbedder:
    """Embedder Protocol impl producing a deterministic vector from the content hash."""

    embedding_model = "local-hash-v1"

    async def embed(self, text: str) -> EmbeddingResult:
        digest = content_hash(text)
        vector = [int(digest[i * 8 : i * 8 + 8], 16) / 0xFFFFFFFF for i in range(_DIMS)]
        return EmbeddingResult(embedding_hash=digest, vector=vector)


__all__ = ["LocalHashEmbedder"]
