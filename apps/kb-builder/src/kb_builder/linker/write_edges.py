"""Persist linker edges idempotently.

Inserts target the partial unique index uq_knowledge_edge_linker
(from, to, edge_type, kb_version WHERE source='linker'), so a rerun within the
same kb_version never duplicates an edge; a conflicting row instead refreshes
its confidence (thresholds may have been retuned between retries). Edges below
LOW_CONFIDENCE_THRESHOLD are written but flagged with a structured log so the
eval harness can audit them — uncertain links are recorded as low confidence,
never silently promoted to facts.
"""

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.logging import get_logger
from contracts.artifact_schemas import LinkEdgeDraft
from db.models import KnowledgeEdge

logger = get_logger("kb_builder.linker.write_edges")

EDGE_SOURCE = "linker"
LOW_CONFIDENCE_THRESHOLD = 0.9


async def write_link_edges(
    session: AsyncSession, *, kb_version: str, drafts: Sequence[LinkEdgeDraft]
) -> tuple[int, int]:
    """Upsert one knowledge_edge row per draft; return (inserted, refreshed).

    Drafts must be unique on (from, to, edge_type) — the linker run dedupes —
    because a single INSERT cannot update the same conflicting row twice.
    """
    inserted = 0
    refreshed = 0
    for draft in drafts:
        if draft.confidence < LOW_CONFIDENCE_THRESHOLD:
            logger.warning(
                "event=linker_low_confidence_edge from=%s to=%s edge_type=%s "
                "confidence=%.3f strategy=%s",
                draft.from_artifact_id,
                draft.to_artifact_id,
                draft.edge_type,
                draft.confidence,
                draft.strategy,
            )
        statement = (
            insert(KnowledgeEdge)
            .values(
                from_artifact_id=draft.from_artifact_id,
                to_artifact_id=draft.to_artifact_id,
                edge_type=draft.edge_type,
                confidence=draft.confidence,
                source=EDGE_SOURCE,
                kb_version=kb_version,
            )
            .on_conflict_do_update(
                index_elements=["from_artifact_id", "to_artifact_id", "edge_type", "kb_version"],
                index_where=text("source = 'linker'"),
                set_={"confidence": draft.confidence},
            )
            .returning(text("(xmax = 0)"))
        )
        was_insert = (await session.execute(statement)).scalar_one()
        if was_insert:
            inserted += 1
        else:
            refreshed += 1
    await session.flush()
    logger.info(
        "event=linker_edges_written kb_version=%s inserted=%d refreshed=%d",
        kb_version,
        inserted,
        refreshed,
    )
    return inserted, refreshed
