"""The crash-durable model-output cache port.

`DurableOutputCache` is the abstraction the build runner depends on to memoise expensive
model outputs in a way that survives a build rollback. The concrete adapter
(`PostgresDurableOutputCache`) side-commits on its own connection; tests use an in-memory
fake. The port lives in the domain so dependencies point inward — the application
(build_runner) depends on this Protocol, never the Postgres adapter.

Distinct from the in-transaction `generation_cache` / `embedding_cache` gates: those replay
within a *completed* build; this layer makes the underlying model output durable the moment
it is produced, so a crashed-and-restarted build does not re-pay for it.
"""

from typing import Protocol

from agentic_kb_builder.domain.docify_artifacts import DocExtractionResult
from agentic_kb_builder.domain.embedding_port import EmbeddingResult


class DurableOutputCache(Protocol):
    """Side-durable cache of raw model outputs (doc extraction + embeddings).

    Implementations MUST persist each ``put_*`` independently of any caller transaction
    (so the row survives a build rollback) and MUST be idempotent (a repeated put for the
    same key is a no-op, never a duplicate or overwrite)."""

    async def get_doc_extraction(self, cache_key: str) -> DocExtractionResult | None: ...

    async def put_doc_extraction(
        self,
        *,
        cache_key: str,
        input_hash: str,
        prompt_version: str,
        model_name: str,
        model_params_hash: str,
        output_schema_version: str,
        result: DocExtractionResult,
    ) -> None: ...

    async def get_embedding(
        self, *, text_hash: str, embedding_model: str
    ) -> EmbeddingResult | None: ...

    async def put_embedding(
        self, *, text_hash: str, embedding_model: str, result: EmbeddingResult
    ) -> None: ...


__all__ = ["DurableOutputCache"]
