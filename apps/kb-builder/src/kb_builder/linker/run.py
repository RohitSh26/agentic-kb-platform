"""Linker orchestration: load artifacts, deterministic pass, semantic fallback.

Scans all non-deleted artifacts (not only this build's) because cache-hit
artifacts keep their original kb_version. The write pass reconciles the
knowledge_edge table with the computed set — one row per logical link,
refreshed in place and deleted when its evidence disappears — so nightly
builds never accrete duplicate or stale linker edges.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.logging import get_logger
from db.models import KnowledgeArtifact, SourceItem
from kb_builder.linker.deterministic import find_deterministic_links
from kb_builder.linker.records import LinkableArtifact
from kb_builder.linker.semantic import SimilarityProvider, find_semantic_links
from kb_builder.linker.write_edges import write_link_edges

logger = get_logger("kb_builder.linker.run")


async def run_linker(
    session: AsyncSession,
    *,
    kb_version: str,
    similarity: SimilarityProvider | None = None,
) -> tuple[int, int, int]:
    """Compute and persist linker edges; return (inserted, refreshed, deleted)."""
    artifacts = await _load_linkable_artifacts(session)
    drafts = find_deterministic_links(artifacts)
    # With no provider the semantic pass is skipped, so its implements edges
    # being absent from the computed set is not evidence they are stale.
    protected = frozenset({"implements"}) if similarity is None else frozenset()
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
    return await write_link_edges(
        session, kb_version=kb_version, drafts=drafts, protected_edge_types=protected
    )


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
