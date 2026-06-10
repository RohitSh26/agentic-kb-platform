"""Persist code artifact drafts and resolve symbolic edge keys to uuids.

Cross-file edge targets are resolved by deterministic DB lookup against
already-persisted artifacts (newest first); unresolved keys drop the edge with
a structured log — never a fabricated node (invariant 7).
"""

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.domain import CodeArtifactDraft, CodeEdgeDraft
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.graphify.keys import parse_key
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Code artifacts are verbatim source-backed evidence, on par with chunks.
CODE_AUTHORITY = 1.0
BUILD_TIME_FRESHNESS = 1.0
EDGE_SOURCE = "graphify"


async def write_code_artifacts(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    kb_version: str,
    drafts: Sequence[CodeArtifactDraft],
) -> dict[str, uuid.UUID]:
    """Insert one knowledge_artifact row per draft; return symbolic key -> uuid.

    Flushes so ids are assigned but does not commit — the build runner owns the
    transaction and records the generation-cache row after this returns.
    """
    rows = [
        KnowledgeArtifact(
            artifact_type=draft.artifact_type,
            source_id=source_id,
            title=draft.title,
            body_text=draft.body_text,
            content_hash=content_hash(draft.body_text) if draft.body_text is not None else None,
            kb_version=kb_version,
            knowledge_kind="source_backed",
            authority_score=CODE_AUTHORITY,
            freshness_score=BUILD_TIME_FRESHNESS,
            span_start=draft.span_start,
            span_end=draft.span_end,
        )
        for draft in drafts
    ]
    session.add_all(rows)
    await session.flush()
    logger.info(
        "event=graphify_artifacts_written source_id=%s kb_version=%s count=%d",
        source_id,
        kb_version,
        len(rows),
    )
    return {draft.key: row.artifact_id for draft, row in zip(drafts, rows, strict=True)}


async def write_code_edges(
    session: AsyncSession,
    *,
    kb_version: str,
    repo: str,
    drafts: Sequence[CodeEdgeDraft],
    key_to_id: Mapping[tuple[str, str], uuid.UUID],
) -> tuple[int, int]:
    """Resolve edge drafts and insert knowledge_edge rows; return (inserted, dropped).

    key_to_id is keyed by (repo, symbolic key): paths are repo-relative, so the
    same key can name different artifacts in different repos within one build.
    """
    inserted = 0
    dropped = 0
    for draft in drafts:
        from_id = await _resolve(session, repo=repo, key=draft.from_key, key_to_id=key_to_id)
        to_id = await _resolve(session, repo=repo, key=draft.to_key, key_to_id=key_to_id)
        if from_id is None or to_id is None:
            dropped += 1
            logger.warning(
                "event=graphify_edge_dropped reason=unresolved_key from_key=%s to_key=%s "
                "edge_type=%s repo=%s",
                draft.from_key,
                draft.to_key,
                draft.edge_type,
                repo,
            )
            continue
        session.add(
            KnowledgeEdge(
                from_artifact_id=from_id,
                to_artifact_id=to_id,
                edge_type=draft.edge_type,
                confidence=draft.confidence,
                source=EDGE_SOURCE,
                kb_version=kb_version,
            )
        )
        inserted += 1
    await session.flush()
    logger.info(
        "event=graphify_edges_written kb_version=%s inserted=%d dropped=%d",
        kb_version,
        inserted,
        dropped,
    )
    return inserted, dropped


async def _resolve(
    session: AsyncSession,
    *,
    repo: str,
    key: str,
    key_to_id: Mapping[tuple[str, str], uuid.UUID],
) -> uuid.UUID | None:
    in_batch = key_to_id.get((repo, key))
    if in_batch is not None:
        return in_batch
    parsed = parse_key(key)
    conditions = [
        KnowledgeArtifact.artifact_type == parsed.artifact_type,
        SourceItem.path == parsed.path,
        SourceItem.repo == repo,
        SourceItem.is_deleted.is_(False),
    ]
    if parsed.title is not None:
        conditions.append(KnowledgeArtifact.title == parsed.title)
    statement = (
        select(KnowledgeArtifact.artifact_id)
        .join(SourceItem, KnowledgeArtifact.source_id == SourceItem.source_id)
        .where(*conditions)
        .order_by(KnowledgeArtifact.created_at.desc(), KnowledgeArtifact.artifact_id.desc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()
