"""Persist docify drafts as knowledge_artifact rows (Postgres is truth).

Writes document artifacts. Docify produces ARTIFACTS ONLY — no edges
: Graphify's concept->concept relations are generic
relatedness, which the relation ontology bans as an edge. Artifacts carry valid_from_seq
and participate in supersession via the existing invalidation pass (a changed source
retires its prior-generation artifacts); no new supersession mechanism is introduced
.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.domain import DocArtifactDraft
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import KnowledgeArtifact
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


async def write_doc_artifacts(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    kb_version: str,
    valid_from_seq: int = 0,
    acl_teams: list[str] | None = None,
    drafts: Sequence[DocArtifactDraft],
) -> list[uuid.UUID]:
    """Insert one knowledge_artifact row per draft; return artifact ids in DRAFT ORDER.

    The ordered id list is what the generation cache records (preserving generation order
    so a replay surfaces the same set in the same order).

    Flushes so ids are assigned, but does not commit — the build runner owns the
    transaction and records the generation-cache row in the same transaction after this
    returns. valid_from_seq stamps the introducing build (interval membership,;
    acl_teams propagates the SOURCE's ACL onto every derived artifact (never widened,
    never from Graphify output —.
    """
    rows = [
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
    session.add_all(rows)
    await session.flush()
    artifact_ids = [row.artifact_id for row in rows]
    logger.info(
        "event=docify_artifacts_written source_id=%s kb_version=%s count=%d",
        source_id,
        kb_version,
        len(rows),
    )
    return artifact_ids


__all__ = ["write_doc_artifacts"]
