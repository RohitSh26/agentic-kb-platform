"""Read-only knowledge_edge access for graph.get_neighbors.

Graph behavior is exposed only through MCP tools (invariant 2); this module is
the single place that knows edges live in a Postgres table.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KNOWLEDGE_EDGE_TABLE = "knowledge_edge"

# Membership predicate (version-membership.md, ADR-0013): an edge is traversable
# iff it is a MEMBER of the active build's build_seq, not iff its label equals the
# active kb_version. (The endpoint artifacts are independently membership-filtered
# by the graph tool's per-hop fetch_artifacts.)
_FETCH_EDGES_QUERY = text(
    f"""
    SELECT from_artifact_id, to_artifact_id, edge_type, confidence, source, trust_class
    FROM {KNOWLEDGE_EDGE_TABLE}
    WHERE valid_from_seq <= :build_seq
      AND (invalidated_at_seq IS NULL OR invalidated_at_seq > :build_seq)
      AND (from_artifact_id = ANY(CAST(:artifact_ids AS uuid[]))
           OR to_artifact_id = ANY(CAST(:artifact_ids AS uuid[])))
      AND (CAST(:edge_types AS text[]) IS NULL OR edge_type = ANY(CAST(:edge_types AS text[])))
    """
)


@dataclass(frozen=True)
class EdgeRow:
    from_artifact_id: uuid.UUID
    to_artifact_id: uuid.UUID
    edge_type: str
    confidence: float | None
    source: str | None
    # Trust bucket (docs/contracts/trust-buckets.md). The broker enforces the
    # trust_floor over this column; an unknown value is treated as AMBIGUOUS.
    trust_class: str


async def fetch_edges_touching(
    session: AsyncSession,
    artifact_ids: list[uuid.UUID],
    build_seq: int,
    edge_types: list[str] | None,
) -> list[EdgeRow]:
    result = await session.execute(
        _FETCH_EDGES_QUERY,
        {
            "artifact_ids": artifact_ids,
            "build_seq": build_seq,
            "edge_types": edge_types or None,
        },
    )
    return [
        EdgeRow(
            from_artifact_id=row.from_artifact_id,
            to_artifact_id=row.to_artifact_id,
            edge_type=row.edge_type,
            confidence=row.confidence,
            source=row.source,
            trust_class=row.trust_class,
        )
        for row in result
    ]
