"""Read-only knowledge_edge access for graph.get_neighbors.

Graph behavior is exposed only through MCP tools (invariant 2); this module is
the single place that knows edges live in a Postgres table.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KNOWLEDGE_EDGE_TABLE = "knowledge_edge"

_FETCH_EDGES_QUERY = text(
    f"""
    SELECT from_artifact_id, to_artifact_id, edge_type, confidence, source
    FROM {KNOWLEDGE_EDGE_TABLE}
    WHERE kb_version = :kb_version
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


async def fetch_edges_touching(
    session: AsyncSession,
    artifact_ids: list[uuid.UUID],
    kb_version: str,
    edge_types: list[str] | None,
) -> list[EdgeRow]:
    result = await session.execute(
        _FETCH_EDGES_QUERY,
        {
            "artifact_ids": artifact_ids,
            "kb_version": kb_version,
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
        )
        for row in result
    ]
