"""Cache keys and gates for the incremental build.

Every LLM/embedding call is gated by a cache key (architecture invariant 4):
cache hit => no model call, no embedding. Key composition follows
docs/architecture §7 "Cache keys" exactly; inserts are idempotent so build
retries never duplicate cache rows.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.hashing import content_hash
from common.logging import get_logger
from db.models import EmbeddingCache, GenerationCache, GenerationCacheArtifact

logger = get_logger("kb_builder.build.cache")

_SEPARATOR = "\x1f"


def _compose_key(kind: str, *parts: str) -> str:
    return content_hash(_SEPARATOR.join((kind, *parts)))


def chunk_summary_cache_key(
    *,
    source_content_hash: str,
    chunker_version: str,
    wikify_prompt_version: str,
    model_name: str,
    model_params_hash: str,
    output_schema_version: str,
) -> str:
    return _compose_key(
        "chunk_summary",
        source_content_hash,
        chunker_version,
        wikify_prompt_version,
        model_name,
        model_params_hash,
        output_schema_version,
    )


def concept_rollup_cache_key(
    *,
    concept_id: str,
    supporting_artifact_hashes: Sequence[str],
    rollup_prompt_version: str,
    model_name: str,
    output_schema_version: str,
) -> str:
    return _compose_key(
        "concept_rollup",
        concept_id,
        *sorted(supporting_artifact_hashes),
        rollup_prompt_version,
        model_name,
        output_schema_version,
    )


def code_graph_cache_key(
    *,
    repo: str,
    commit_sha: str,
    file_path: str,
    file_content_hash: str,
    graphify_version: str,
    parser_config_version: str,
) -> str:
    return _compose_key(
        "code_graph",
        repo,
        commit_sha,
        file_path,
        file_content_hash,
        graphify_version,
        parser_config_version,
    )


class GenerationCacheGate:
    """Lookup/record gate over generation_cache; a hit must prevent the LLM call."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lookup(self, cache_key: str) -> GenerationCache | None:
        hit = await self._session.get(GenerationCache, cache_key)
        logger.info("event=generation_cache_lookup cache_key=%s hit=%s", cache_key, hit is not None)
        return hit

    async def lookup_artifact_ids(self, cache_key: str) -> list[uuid.UUID]:
        """All output artifact ids for a cache row, in generation order.

        generation_cache_artifact is the single source of truth on hits;
        generation_cache.output_artifact_id is denormalized and never read here.
        """
        rows = (
            await self._session.execute(
                select(GenerationCacheArtifact.artifact_id)
                .where(GenerationCacheArtifact.cache_key == cache_key)
                .order_by(GenerationCacheArtifact.position)
            )
        ).scalars()
        return list(rows)

    async def record(
        self,
        *,
        cache_key: str,
        input_hash: str,
        prompt_version: str,
        model_name: str,
        model_params_hash: str,
        output_schema_version: str,
        output_artifact_ids: Sequence[uuid.UUID],
    ) -> None:
        """Idempotent insert; call only after the output artifact rows are persisted
        in the same transaction, or a retry would hit the cache and find nothing."""
        statement = (
            insert(GenerationCache)
            .values(
                cache_key=cache_key,
                input_hash=input_hash,
                prompt_version=prompt_version,
                model_name=model_name,
                model_params_hash=model_params_hash,
                output_schema_version=output_schema_version,
                # denormalized copy of position 0 (architecture §6 sketch); the
                # hit path reads generation_cache_artifact instead.
                output_artifact_id=output_artifact_ids[0] if output_artifact_ids else None,
            )
            .on_conflict_do_nothing(index_elements=["cache_key"])
        )
        await self._session.execute(statement)
        if output_artifact_ids:
            # Retry-idempotent for the same ids. Two CONCURRENT builders missing
            # the same key would record different ids and the loser aborts on
            # uq_(cache_key, position) (not covered by the PK conflict target) —
            # loud, not silent; fine for the single nightly runner.
            mapping = (
                insert(GenerationCacheArtifact)
                .values(
                    [
                        {"cache_key": cache_key, "artifact_id": artifact_id, "position": position}
                        for position, artifact_id in enumerate(output_artifact_ids)
                    ]
                )
                .on_conflict_do_nothing(index_elements=["cache_key", "artifact_id"])
            )
            await self._session.execute(mapping)
        logger.info(
            "event=generation_cache_record cache_key=%s output_artifacts=%d",
            cache_key,
            len(output_artifact_ids),
        )


class EmbeddingCacheGate:
    """Lookup/record gate over embedding_cache; a hit must prevent re-embedding."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lookup(
        self, *, artifact_id: uuid.UUID, text_hash: str, embedding_model: str
    ) -> EmbeddingCache | None:
        hit = await self._session.get(EmbeddingCache, (artifact_id, text_hash, embedding_model))
        logger.info(
            "event=embedding_cache_lookup artifact_id=%s text_hash=%s model=%s hit=%s",
            artifact_id,
            text_hash,
            embedding_model,
            hit is not None,
        )
        return hit

    async def record(
        self,
        *,
        artifact_id: uuid.UUID,
        text_hash: str,
        embedding_model: str,
        embedding_hash: str,
        azure_search_doc_id: str | None = None,
    ) -> None:
        statement = (
            insert(EmbeddingCache)
            .values(
                artifact_id=artifact_id,
                text_hash=text_hash,
                embedding_model=embedding_model,
                embedding_hash=embedding_hash,
                azure_search_doc_id=azure_search_doc_id,
            )
            .on_conflict_do_nothing(index_elements=["artifact_id", "text_hash", "embedding_model"])
        )
        await self._session.execute(statement)
        logger.info(
            "event=embedding_cache_record artifact_id=%s text_hash=%s model=%s",
            artifact_id,
            text_hash,
            embedding_model,
        )
