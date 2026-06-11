"""Read-only knowledge_artifact access for the broker.

Raw SQL with pinned names, same pattern as active_kb_version.py: no ORM model
crosses the service boundary; docs/contracts/postgres-knowledge-registry.md is
the contract and the contract tests keep these constants honest.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KNOWLEDGE_ARTIFACT_TABLE = "knowledge_artifact"
SOURCE_ITEM_TABLE = "source_item"

_FETCH_ARTIFACTS_QUERY = text(
    f"""
    SELECT a.artifact_id, a.artifact_type, a.title, a.body_text, a.knowledge_kind,
           a.authority_score, a.acl_teams, s.source_uri
    FROM {KNOWLEDGE_ARTIFACT_TABLE} a
    JOIN {SOURCE_ITEM_TABLE} s ON s.source_id = a.source_id
    WHERE a.artifact_id = ANY(CAST(:artifact_ids AS uuid[]))
      AND a.kb_version = :kb_version
    """
)


@dataclass(frozen=True)
class ArtifactRow:
    artifact_id: uuid.UUID
    artifact_type: str
    title: str | None
    body_text: str | None
    knowledge_kind: str | None
    authority_score: float | None
    source_uri: str
    # empty = org-public; non-empty = requester team set must intersect
    acl_teams: tuple[str, ...] = ()


async def fetch_artifacts(
    session: AsyncSession, artifact_ids: list[uuid.UUID], kb_version: str
) -> list[ArtifactRow]:
    result = await session.execute(
        _FETCH_ARTIFACTS_QUERY, {"artifact_ids": artifact_ids, "kb_version": kb_version}
    )
    return [
        ArtifactRow(
            artifact_id=row.artifact_id,
            artifact_type=row.artifact_type,
            title=row.title,
            body_text=row.body_text,
            knowledge_kind=row.knowledge_kind,
            authority_score=row.authority_score,
            source_uri=row.source_uri,
            acl_teams=tuple(row.acl_teams),
        )
        for row in result
    ]
