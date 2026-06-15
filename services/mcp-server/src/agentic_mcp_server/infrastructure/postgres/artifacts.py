"""Read-only knowledge_artifact access for the broker.

Raw SQL with pinned names, same pattern as active_kb_version.py: no ORM model
crosses the service boundary; docs/contracts/postgres-knowledge-registry.md is
the contract and the contract tests keep these constants honest.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_mcp_server.structured_logging import get_logger

logger = get_logger(__name__)

KNOWLEDGE_ARTIFACT_TABLE = "knowledge_artifact"
SOURCE_ITEM_TABLE = "source_item"

# Membership predicate (version-membership.md, ADR-0013): a row is served iff it is
# a MEMBER of the active build's build_seq, NOT iff its label equals the active
# kb_version. valid_from_seq <= S AND (invalidated_at_seq IS NULL OR > S).
_FETCH_ARTIFACTS_QUERY = text(
    f"""
    SELECT a.artifact_id, a.artifact_type, a.title, a.body_text, a.knowledge_kind,
           a.authority_score, a.acl_teams, a.invalidated_at_seq,
           s.source_uri, s.source_type, s.is_deleted
    FROM {KNOWLEDGE_ARTIFACT_TABLE} a
    JOIN {SOURCE_ITEM_TABLE} s ON s.source_id = a.source_id
    WHERE a.artifact_id = ANY(CAST(:artifact_ids AS uuid[]))
      AND a.valid_from_seq <= :build_seq
      AND (a.invalidated_at_seq IS NULL OR a.invalidated_at_seq > :build_seq)
    """
)

# Current code-symbol titles that are MEMBERS of the active build_seq. PR-33's
# stale-doc signal compares a doc's referenced symbols against this set: a doc
# that names a symbol absent here references a removed/absent symbol and is
# downranked for `how_does_x_work` (a routing hint, never primary). Derived from
# already-stored data — no LLM, no schema change. NULL titles are excluded.
_CODE_SYMBOL_TITLES_TABLE = "knowledge_artifact"
_FETCH_CURRENT_SYMBOL_TITLES_QUERY = text(
    f"""
    SELECT DISTINCT a.title
    FROM {_CODE_SYMBOL_TITLES_TABLE} a
    JOIN {SOURCE_ITEM_TABLE} s ON s.source_id = a.source_id
    WHERE a.artifact_type IN ('code_symbol', 'code_file', 'endpoint')
      AND a.title IS NOT NULL
      -- exclude symbols whose SOURCE was deleted, matching fetch_artifacts, so a
      -- removed symbol can't read as "current" and falsely clear a stale-doc flag
      AND s.is_deleted = false
      AND a.valid_from_seq <= :build_seq
      AND (a.invalidated_at_seq IS NULL OR a.invalidated_at_seq > :build_seq)
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
    # Temporal-derivation inputs (PR-33). All already-stored; no new generation.
    # source_type drives the source KIND; invalidated_at_seq + source_is_deleted
    # drive the current/superseded state. These are RANKING signals only and do
    # not affect membership (the WHERE clause already enforced membership).
    source_type: str | None = None
    invalidated_at_seq: int | None = None
    source_is_deleted: bool = False


async def fetch_artifacts(
    session: AsyncSession, artifact_ids: list[uuid.UUID], build_seq: int
) -> list[ArtifactRow]:
    """Return the requested artifacts that are MEMBERS of the active `build_seq`.

    Filters by interval membership (version-membership.md), so an artifact
    introduced by an earlier build but still live is served, and an artifact
    invalidated by the active build is not.
    """
    result = await session.execute(
        _FETCH_ARTIFACTS_QUERY, {"artifact_ids": artifact_ids, "build_seq": build_seq}
    )
    artifacts = [
        ArtifactRow(
            artifact_id=row.artifact_id,
            artifact_type=row.artifact_type,
            title=row.title,
            body_text=row.body_text,
            knowledge_kind=row.knowledge_kind,
            authority_score=row.authority_score,
            source_uri=row.source_uri,
            acl_teams=tuple(row.acl_teams),
            source_type=row.source_type,
            invalidated_at_seq=row.invalidated_at_seq,
            source_is_deleted=row.is_deleted,
        )
        for row in result
    ]
    requested = len(set(artifact_ids))
    if len(artifacts) < requested:
        # callers treat a missing row as unauthorized/unknown, so a build-plane
        # anomaly (orphaned or source-deleted artifact) would otherwise vanish
        # silently from every retrieval surface (python.md: no silent failures)
        logger.warning(
            "event=fetch_artifacts_incomplete requested=%d returned=%d build_seq=%d",
            requested,
            len(artifacts),
            build_seq,
        )
    return artifacts


async def fetch_current_symbol_titles(session: AsyncSession, build_seq: int) -> set[str]:
    """Titles of code symbols/files that are MEMBERS of the active `build_seq`.

    The reference set PR-33's stale-doc signal compares doc references against: a
    doc that names a symbol NOT in this set references a removed/absent symbol.
    Read-only over already-stored data (no LLM); membership-filtered like
    fetch_artifacts so a symbol removed by a later build is not counted current.
    """
    result = await session.execute(_FETCH_CURRENT_SYMBOL_TITLES_QUERY, {"build_seq": build_seq})
    return {row.title for row in result if row.title}
