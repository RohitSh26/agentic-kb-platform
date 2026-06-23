"""Postgres adapter for the crash-durable model-output cache (ADR-0027).

Owns its OWN engine + session factory, separate from the build session, so each write is
committed on its own connection the moment the model returns — independent of the build
transaction and therefore surviving a build rollback. A re-run after a crash reads the
side-committed output and re-maps it with zero model calls.

Writes are idempotent (on-conflict-do-nothing). The engine MUST be disposed via
``aclose()`` in a ``finally`` so a crash never leaks a pool connection.
"""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_kb_builder.domain.docify_artifacts import DocExtractionResult
from agentic_kb_builder.domain.embedding_port import EmbeddingResult
from agentic_kb_builder.infrastructure.postgres.models import (
    DocExtractionOutput,
    EmbeddingOutput,
)
from agentic_kb_builder.infrastructure.postgres.session import (
    create_engine,
    create_session_factory,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


class PostgresDurableOutputCache:
    """Side-committing durable cache; satisfies the `DurableOutputCache` port."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._factory = create_session_factory(engine)
        # The durable cache is an OPTIMIZATION, never load-bearing for build success. If its
        # storage is unavailable (tables not migrated, transient DB error), the build must still
        # complete — it just won't cache model outputs for crash-resume. We log one loud,
        # actionable warning and then degrade silently for the rest of the run.
        self._degraded = False

    def _degrade(self, op: str, exc: Exception) -> None:
        if not self._degraded:
            self._degraded = True
            logger.warning(
                "event=durable_cache_unavailable op=%s error=%s — the build will COMPLETE but "
                "model outputs are NOT being cached for crash-resume. Run 'alembic upgrade head' "
                "to create the durable cache tables and enable token-free re-runs.",
                op,
                f"{type(exc).__name__}: {exc}",
            )

    @classmethod
    def from_url(cls, url: str | None = None) -> "PostgresDurableOutputCache":
        """Build a cache on its OWN engine/connection pool (not the build engine)."""
        return cls(create_engine(url))

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def get_doc_extraction(self, cache_key: str) -> DocExtractionResult | None:
        try:
            async with self._factory() as session:
                row = await session.get(DocExtractionOutput, cache_key)
                if row is None:
                    return None
                # read the column inside the session so the result never depends on a
                # detached/expired instance after the block exits.
                output_json = row.output_json
        except SQLAlchemyError as exc:
            self._degrade("get_doc_extraction", exc)
            return None  # treat as a miss; the caller pays the model (build still proceeds)
        logger.info("event=durable_doc_extraction_hit cache_key=%s", cache_key)
        return DocExtractionResult.model_validate(output_json)

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
    ) -> None:
        statement = (
            insert(DocExtractionOutput)
            .values(
                cache_key=cache_key,
                input_hash=input_hash,
                prompt_version=prompt_version,
                model_name=model_name,
                model_params_hash=model_params_hash,
                output_schema_version=output_schema_version,
                output_json=result.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=["cache_key"])
        )
        try:
            async with self._factory() as session:
                await session.execute(statement)
                await session.commit()
        except SQLAlchemyError as exc:
            self._degrade("put_doc_extraction", exc)
            return  # the build's own transaction is untouched (separate connection)
        logger.info("event=durable_doc_extraction_put cache_key=%s", cache_key)

    async def get_embedding(
        self, *, text_hash: str, embedding_model: str
    ) -> EmbeddingResult | None:
        try:
            async with self._factory() as session:
                row = await session.get(EmbeddingOutput, (text_hash, embedding_model))
                if row is None:
                    return None
                # read the columns inside the session (see get_doc_extraction).
                result = EmbeddingResult(
                    embedding_hash=row.embedding_hash, vector=list(row.embedding)
                )
        except SQLAlchemyError as exc:
            self._degrade("get_embedding", exc)
            return None
        logger.info(
            "event=durable_embedding_hit text_hash=%s model=%s", text_hash, embedding_model
        )
        return result

    async def put_embedding(
        self, *, text_hash: str, embedding_model: str, result: EmbeddingResult
    ) -> None:
        statement = (
            insert(EmbeddingOutput)
            .values(
                text_hash=text_hash,
                embedding_model=embedding_model,
                embedding_hash=result.embedding_hash,
                embedding=result.vector,
            )
            .on_conflict_do_nothing(index_elements=["text_hash", "embedding_model"])
        )
        try:
            async with self._factory() as session:
                await session.execute(statement)
                await session.commit()
        except SQLAlchemyError as exc:
            self._degrade("put_embedding", exc)
            return
        logger.info(
            "event=durable_embedding_put text_hash=%s model=%s", text_hash, embedding_model
        )


def make_durable_output_cache(url: str | None = None) -> PostgresDurableOutputCache:
    """Factory used by the build entrypoint; isolates construction (one call site)."""
    return PostgresDurableOutputCache.from_url(url)


__all__ = ["PostgresDurableOutputCache", "make_durable_output_cache"]
