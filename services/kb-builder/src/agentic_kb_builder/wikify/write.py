"""Persist wikify drafts as knowledge_artifact rows (Postgres is truth)."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.domain import WikifyArtifactDraft
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import KnowledgeArtifact
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


async def write_wikify_artifacts(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    kb_version: str,
    drafts: Sequence[WikifyArtifactDraft],
) -> list[uuid.UUID]:
    """Insert one knowledge_artifact row per draft and return ids in draft order.

    Flushes so ids are assigned, but does not commit — the caller (the build
    runner) owns the transaction and must record the generation-cache row in
    the same transaction after this returns.
    """
    artifacts = [
        KnowledgeArtifact(
            artifact_type=draft.artifact_type,
            source_id=source_id,
            title=draft.title,
            body_text=draft.body_text,
            content_hash=content_hash(draft.body_text),
            kb_version=kb_version,
            knowledge_kind=draft.knowledge_kind,
            authority_score=draft.authority_score,
            freshness_score=draft.freshness_score,
        )
        for draft in drafts
    ]
    session.add_all(artifacts)
    await session.flush()
    artifact_ids = [artifact.artifact_id for artifact in artifacts]
    logger.info(
        "event=wikify_artifacts_written source_id=%s kb_version=%s count=%d",
        source_id,
        kb_version,
        len(artifact_ids),
    )
    return artifact_ids
