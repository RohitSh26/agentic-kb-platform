"""Linker orchestration: load artifacts, deterministic pass, semantic fallback.

Scans all non-deleted artifacts (not only this build's) because cache-hit
artifacts keep their original kb_version. If the computed edge set is identical
to the previous version's, the write is skipped entirely so nightly builds do
not grow knowledge_edge by a full copy per night — matching graphify, where
unchanged sources keep their previously written edges and kb_version records
when an edge was created, not a serving filter.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.logging import get_logger
from contracts.artifact_schemas import LinkEdgeDraft
from db.models import KnowledgeArtifact, KnowledgeEdge, SourceItem
from kb_builder.linker.deterministic import find_deterministic_links
from kb_builder.linker.records import LinkableArtifact
from kb_builder.linker.semantic import SimilarityProvider, find_semantic_links
from kb_builder.linker.write_edges import EDGE_SOURCE, write_link_edges

logger = get_logger("kb_builder.linker.run")


async def run_linker(
    session: AsyncSession,
    *,
    kb_version: str,
    similarity: SimilarityProvider | None = None,
) -> tuple[int, int]:
    """Compute and persist linker edges; return (inserted, refreshed)."""
    artifacts = await _load_linkable_artifacts(session)
    drafts = find_deterministic_links(artifacts)
    if similarity is None:
        logger.info("event=linker_semantic_skipped reason=no_provider")
    else:
        linked_concept_ids = {d.to_artifact_id for d in drafts if d.edge_type == "implements"}
        unlinked = [
            a
            for a in artifacts
            if a.artifact_type == "concept" and a.artifact_id not in linked_concept_ids
        ]
        existing_pairs = {(d.from_artifact_id, d.to_artifact_id, str(d.edge_type)) for d in drafts}
        drafts += await find_semantic_links(similarity, unlinked, existing_pairs=existing_pairs)
    if await _unchanged_since_previous_version(session, kb_version=kb_version, drafts=drafts):
        return 0, 0
    return await write_link_edges(session, kb_version=kb_version, drafts=drafts)


async def _load_linkable_artifacts(session: AsyncSession) -> list[LinkableArtifact]:
    rows = await session.execute(
        select(
            KnowledgeArtifact.artifact_id,
            KnowledgeArtifact.artifact_type,
            KnowledgeArtifact.title,
            KnowledgeArtifact.body_text,
            SourceItem.source_type,
        )
        .join(SourceItem, KnowledgeArtifact.source_id == SourceItem.source_id)
        .where(SourceItem.is_deleted.is_(False))
    )
    return [LinkableArtifact(*row) for row in rows.tuples()]


async def _unchanged_since_previous_version(
    session: AsyncSession, *, kb_version: str, drafts: list[LinkEdgeDraft]
) -> bool:
    previous_version = (
        await session.execute(
            select(KnowledgeEdge.kb_version)
            .where(KnowledgeEdge.source == EDGE_SOURCE, KnowledgeEdge.kb_version != kb_version)
            .order_by(KnowledgeEdge.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if previous_version is None:
        return False
    rows = await session.execute(
        select(
            KnowledgeEdge.from_artifact_id,
            KnowledgeEdge.to_artifact_id,
            KnowledgeEdge.edge_type,
            KnowledgeEdge.confidence,
        ).where(KnowledgeEdge.source == EDGE_SOURCE, KnowledgeEdge.kb_version == previous_version)
    )
    previous: set[tuple[uuid.UUID, uuid.UUID, str, float | None]] = set(rows.tuples())
    computed = {
        (d.from_artifact_id, d.to_artifact_id, str(d.edge_type), d.confidence) for d in drafts
    }
    if previous != computed:
        return False
    logger.info(
        "event=linker_edges_unchanged previous_kb_version=%s kb_version=%s count=%d",
        previous_version,
        kb_version,
        len(computed),
    )
    return True
