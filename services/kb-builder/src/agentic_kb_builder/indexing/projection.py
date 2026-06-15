"""Project registry artifacts into SearchDocs (architecture §6, ADR-0002).

The projection is a pure read of Postgres: artifact + source pointer +
cached embedding vector. Given the same registry state it always produces the
same documents, which is what makes the index rebuildable and the drift check
meaningful. Only PROJECTABLE_ARTIFACT_TYPES with body_text are projected;
pointer-only code artifacts are reachable through graph edges, not search.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.indexing.search_document import PROJECTABLE_ARTIFACT_TYPES, SearchDoc
from agentic_kb_builder.infrastructure.postgres.models import (
    EmbeddingCache,
    KnowledgeArtifact,
    SourceItem,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


async def load_search_docs(
    session: AsyncSession, *, artifact_ids: Sequence[uuid.UUID] | None = None
) -> list[SearchDoc]:
    """Load SearchDocs for the given artifacts, or every projectable artifact.

    artifact_ids=None is the full-rebuild path; passing ids is the nightly
    changed-only path. Non-projectable ids are silently skipped so callers can
    pass a build's full artifact id list.
    """
    query = (
        select(KnowledgeArtifact, SourceItem.source_type, SourceItem.source_uri)
        .join(SourceItem, KnowledgeArtifact.source_id == SourceItem.source_id)
        .where(
            SourceItem.is_deleted.is_(False),
            KnowledgeArtifact.artifact_type.in_(sorted(PROJECTABLE_ARTIFACT_TYPES)),
            KnowledgeArtifact.body_text.is_not(None),
            # Only LIVE artifacts project: a superseded row (invalidated by a later
            # build) must not stay in the index/expected set (interval membership).
            KnowledgeArtifact.invalidated_at_seq.is_(None),
        )
    )
    if artifact_ids is not None:
        query = query.where(KnowledgeArtifact.artifact_id.in_(artifact_ids))
    rows = (await session.execute(query)).all()
    embeddings = await _load_embeddings(session, [row[0].artifact_id for row in rows])

    docs: list[SearchDoc] = []
    for artifact, source_type, source_uri in rows:
        body_text = artifact.body_text
        assert body_text is not None  # filtered above
        embedding_row = embeddings.get((artifact.artifact_id, content_hash(body_text)))
        vector = embedding_row.embedding if embedding_row is not None else None
        docs.append(
            SearchDoc(
                doc_id=SearchDoc.doc_id_for(artifact.artifact_id),
                artifact_id=artifact.artifact_id,
                artifact_type=artifact.artifact_type,
                source_type=source_type,
                source_uri=source_uri,
                title=artifact.title,
                body_text=body_text,
                kb_version=artifact.kb_version,
                knowledge_kind=artifact.knowledge_kind,
                authority_score=artifact.authority_score,
                freshness_score=artifact.freshness_score,
                artifact_hash=artifact.artifact_hash,
                embedding=tuple(vector) if vector is not None else None,
                embedding_model=(
                    embedding_row.embedding_model if embedding_row is not None else None
                ),
            )
        )
    logger.info(
        "event=indexer_projection_loaded requested=%s projected=%d with_embedding=%d",
        "all" if artifact_ids is None else len(artifact_ids),
        len(docs),
        sum(1 for d in docs if d.embedding is not None),
    )
    return docs


async def load_doc_hashes(session: AsyncSession) -> dict[str, str | None]:
    """Map doc_id -> artifact_hash for every projectable artifact.

    The orphan sweep and the consistency check need only document identity and
    hash, so this deliberately loads neither body_text nor the embedding vectors
    that load_search_docs must materialize — a scalar two-column scan instead of
    full artifact rows joined to the embedding cache.
    """
    query = (
        select(KnowledgeArtifact.artifact_id, KnowledgeArtifact.artifact_hash)
        .join(SourceItem, KnowledgeArtifact.source_id == SourceItem.source_id)
        .where(
            SourceItem.is_deleted.is_(False),
            KnowledgeArtifact.artifact_type.in_(sorted(PROJECTABLE_ARTIFACT_TYPES)),
            KnowledgeArtifact.body_text.is_not(None),
            # Only LIVE artifacts are expected/reconcilable: a superseded row must
            # not be in the consistency "expected" set nor be back-filled by
            # reconcile_missing (interval membership).
            KnowledgeArtifact.invalidated_at_seq.is_(None),
        )
    )
    rows = (await session.execute(query)).all()
    hashes = {
        SearchDoc.doc_id_for(artifact_id): artifact_hash for artifact_id, artifact_hash in rows
    }
    logger.info("event=indexer_doc_hashes_loaded count=%d", len(hashes))
    return hashes


async def _load_embeddings(
    session: AsyncSession, artifact_ids: Sequence[uuid.UUID]
) -> dict[tuple[uuid.UUID, str], EmbeddingCache]:
    """Map (artifact_id, text_hash) to its cache row, preferring stored vectors.

    The text_hash key guarantees the vector matches the *current* body text:
    a stale cache row from an older body never attaches to a newer document.
    """
    if not artifact_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(EmbeddingCache)
                .where(EmbeddingCache.artifact_id.in_(artifact_ids))
                # deterministic winner when several models embedded the same
                # text: the projection must be a pure function of registry state
                .order_by(EmbeddingCache.embedding_model)
            )
        )
        .scalars()
        .all()
    )
    by_key: dict[tuple[uuid.UUID, str], EmbeddingCache] = {}
    for row in rows:
        key = (row.artifact_id, row.text_hash)
        current = by_key.get(key)
        if current is None or (current.embedding is None and row.embedding is not None):
            by_key[key] = row
    return by_key
