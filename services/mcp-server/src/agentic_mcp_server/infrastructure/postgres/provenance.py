"""Read-only provenance facts for the L0 verifier (ADR-0011, phase 1).

Raw SQL with pinned names, same pattern as artifacts.py: no ORM model crosses
the service boundary; docs/contracts/postgres-knowledge-registry.md is the
contract and the contract tests keep these constants honest. This module is the
single place that knows L0's provenance signals live across knowledge_artifact,
source_item, and knowledge_edge.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KNOWLEDGE_ARTIFACT_TABLE = "knowledge_artifact"
SOURCE_ITEM_TABLE = "source_item"
KNOWLEDGE_EDGE_TABLE = "knowledge_edge"

# Per cited id within the served version (by interval membership, ADR-0013):
# existence, ACL set, source state (is_deleted ⇒ superseded/deleted ⇒ stale), and
# whether at least one incident edge is claim-supporting (EXTRACTED). The artifact
# and both edge subqueries filter by the SAME membership predicate against the
# active build_seq, never kb_version label-equality. The edge join is left so an
# artifact with no incident edge still returns a row (supporting_trust_ok = false).
_FETCH_PROVENANCE_QUERY = text(
    f"""
    SELECT a.artifact_id,
           s.is_deleted,
           a.acl_teams,
           EXISTS (
               SELECT 1 FROM {KNOWLEDGE_EDGE_TABLE} e
               WHERE e.valid_from_seq <= :build_seq
                 AND (e.invalidated_at_seq IS NULL OR e.invalidated_at_seq > :build_seq)
                 AND e.trust_class = :extracted
                 AND (e.from_artifact_id = a.artifact_id
                      OR e.to_artifact_id = a.artifact_id)
           ) AS has_extracted_edge,
           EXISTS (
               SELECT 1 FROM {KNOWLEDGE_EDGE_TABLE} e
               WHERE e.valid_from_seq <= :build_seq
                 AND (e.invalidated_at_seq IS NULL OR e.invalidated_at_seq > :build_seq)
                 AND (e.from_artifact_id = a.artifact_id
                      OR e.to_artifact_id = a.artifact_id)
           ) AS has_any_edge
    FROM {KNOWLEDGE_ARTIFACT_TABLE} a
    JOIN {SOURCE_ITEM_TABLE} s ON s.source_id = a.source_id
    WHERE a.artifact_id = ANY(CAST(:artifact_ids AS uuid[]))
      AND a.valid_from_seq <= :build_seq
      AND (a.invalidated_at_seq IS NULL OR a.invalidated_at_seq > :build_seq)
    """
)

# Existence anywhere (any kb_version): lets the verifier distinguish "from
# another version" from "does not exist at all" without leaking which version.
_EXISTS_ANY_VERSION_QUERY = text(
    f"""
    SELECT DISTINCT artifact_id
    FROM {KNOWLEDGE_ARTIFACT_TABLE}
    WHERE artifact_id = ANY(CAST(:artifact_ids AS uuid[]))
    """
)


@dataclass(frozen=True)
class ProvenanceRow:
    artifact_id: uuid.UUID
    # source superseded/deleted in the active version ⇒ stale.
    source_is_deleted: bool
    # empty = org-public; non-empty = requester team set must intersect.
    acl_teams: tuple[str, ...]
    # at least one incident EXTRACTED edge ⇒ claim-supporting trust.
    has_extracted_edge: bool
    # at least one incident edge of any trust ⇒ used to tell "no edges" (standalone
    # source-backed evidence, claim-supporting) from "only inferred edges" (not).
    has_any_edge: bool


async def fetch_provenance(
    session: AsyncSession,
    artifact_ids: list[uuid.UUID],
    build_seq: int,
    *,
    extracted_bucket: str,
) -> dict[uuid.UUID, ProvenanceRow]:
    result = await session.execute(
        _FETCH_PROVENANCE_QUERY,
        {
            "artifact_ids": artifact_ids,
            "build_seq": build_seq,
            "extracted": extracted_bucket,
        },
    )
    return {
        row.artifact_id: ProvenanceRow(
            artifact_id=row.artifact_id,
            source_is_deleted=row.is_deleted,
            acl_teams=tuple(row.acl_teams or ()),
            has_extracted_edge=row.has_extracted_edge,
            has_any_edge=row.has_any_edge,
        )
        for row in result
    }


async def fetch_existing_anywhere(
    session: AsyncSession, artifact_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    result = await session.execute(_EXISTS_ANY_VERSION_QUERY, {"artifact_ids": artifact_ids})
    return {row.artifact_id for row in result}
