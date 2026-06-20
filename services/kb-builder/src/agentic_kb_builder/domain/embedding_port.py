"""The embedding port (domain abstraction).

`Embedder` is the interface the build runner depends on; the concrete embedders
(local hash, Ollama) are adapters that implement it. The port lives in the domain so
dependencies point inward: both the application (build_runner) and the infrastructure
adapters depend on this, and the adapters never import the application layer.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmbeddingResult:
    """The vector is persisted in embedding_cache so the Search index stays
    rebuildable from Postgres without re-embedding (invariant 1/4)."""

    embedding_hash: str
    vector: list[float]


class Embedder(Protocol):
    embedding_model: str

    async def embed(self, text: str) -> EmbeddingResult: ...
