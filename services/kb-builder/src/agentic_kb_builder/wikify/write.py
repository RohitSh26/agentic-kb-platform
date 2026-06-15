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
    valid_from_seq: int = 0,
    acl_teams: list[str] | None = None,
    drafts: Sequence[WikifyArtifactDraft],
) -> list[uuid.UUID]:
    """Insert one knowledge_artifact row per draft and return ids in draft order.

    Flushes so ids are assigned, but does not commit — the caller (the build
    runner) owns the transaction and must record the generation-cache row in
    the same transaction after this returns.

    valid_from_seq stamps the introducing build (interval membership, ADR-0013);
    acl_teams propagates the source's ACL onto the derived artifact (a derived
    artifact is visible only where its source is — closes the acl-propagation
    TODO for newly written rows; the invalidation pass propagates ACL onto
    cache-hit-carried rows whose source ACL changed).
    """
    artifacts = [
        KnowledgeArtifact(
            artifact_type=draft.artifact_type,
            source_id=source_id,
            title=draft.title,
            body_text=draft.body_text,
            content_hash=content_hash(draft.body_text),
            kb_version=kb_version,
            valid_from_seq=valid_from_seq,
            acl_teams=list(acl_teams or []),
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
