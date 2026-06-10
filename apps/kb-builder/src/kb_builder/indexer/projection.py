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

from common.hashing import content_hash
from common.logging import get_logger
from contracts.search_schemas import PROJECTABLE_ARTIFACT_TYPES, SearchDoc
from db.models import EmbeddingCache, KnowledgeArtifact, SourceItem

logger = get_logger("kb_builder.indexer.projection")


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
