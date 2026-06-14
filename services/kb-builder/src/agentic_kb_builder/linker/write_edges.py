"""Persist linker edges as one row per logical link.

Upserts target the partial unique index uq_knowledge_edge_linker
(from, to, edge_type WHERE source='linker'): a rerun refreshes confidence and
kb_version in place instead of accreting a copy per version, so graph queries
never see duplicates. Linker edges absent from the computed set are deleted —
their textual evidence is gone, and serving them would fabricate links
(invariant 7). Edges below LOW_CONFIDENCE_THRESHOLD are written but flagged
with a structured log so the eval harness can audit them — uncertain links are
recorded as low confidence, never silently promoted to facts.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.domain import LinkEdgeDraft
from agentic_kb_builder.domain.schema_versions import RELATION_SCHEMA_VERSION
from agentic_kb_builder.infrastructure.postgres.models import KnowledgeEdge
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

EDGE_SOURCE = "linker"
LOW_CONFIDENCE_THRESHOLD = 0.9
# The deterministic linker may ONLY ever assign EXTRACTED
# (docs/contracts/trust-buckets.md). INFERRED_* arrives with the phase-3 judge.
EDGE_TRUST_CLASS = "EXTRACTED"


async def write_link_edges(
    session: AsyncSession,
    *,
    kb_version: str,
    drafts: Sequence[LinkEdgeDraft],
    protected_edge_types: frozenset[str] = frozenset(),
) -> tuple[int, int, int]:
    """Reconcile linker edges with the computed set; return (inserted, refreshed, deleted).

    Drafts must be unique on (from, to, edge_type) — the linker run dedupes —
    because a single INSERT cannot update the same conflicting row twice.
    protected_edge_types are exempt from stale deletion: when a pass that
    produces them was skipped this run, their absence from the computed set is
    not evidence they are gone.
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
                trust_class=EDGE_TRUST_CLASS,
                relation_schema_version=RELATION_SCHEMA_VERSION,
                evidence=draft.evidence,
            )
            .on_conflict_do_update(
                index_elements=["from_artifact_id", "to_artifact_id", "edge_type"],
                index_where=text("source = 'linker'"),
                set_={
                    "confidence": draft.confidence,
                    "kb_version": kb_version,
                    "relation_schema_version": RELATION_SCHEMA_VERSION,
                    "evidence": draft.evidence,
                },
            )
            .returning(text("(xmax = 0)"))
        )
        was_insert = (await session.execute(statement)).scalar_one()
        if was_insert:
            inserted += 1
        else:
            refreshed += 1
    deleted = await _delete_stale(session, drafts, protected_edge_types)
    await session.flush()
    logger.info(
        "event=linker_edges_written kb_version=%s inserted=%d refreshed=%d deleted=%d",
        kb_version,
        inserted,
        refreshed,
        deleted,
    )
    return inserted, refreshed, deleted


async def _delete_stale(
    session: AsyncSession,
    drafts: Sequence[LinkEdgeDraft],
    protected_edge_types: frozenset[str],
) -> int:
    computed: set[tuple[uuid.UUID, uuid.UUID, str]] = {
        (d.from_artifact_id, d.to_artifact_id, str(d.edge_type)) for d in drafts
    }
    # V1 bound: loads every linker edge into memory to diff against the computed set.
    # Fine at nightly scale; replace with a server-side anti-join (DELETE ... WHERE NOT
    # EXISTS) if the edge count grows large (recorded perf follow-up, KB-4 / #24).
    rows = await session.execute(
        select(
            KnowledgeEdge.edge_id,
            KnowledgeEdge.from_artifact_id,
            KnowledgeEdge.to_artifact_id,
            KnowledgeEdge.edge_type,
            KnowledgeEdge.evidence,
        ).where(KnowledgeEdge.source == EDGE_SOURCE)
    )
    stale_ids: list[uuid.UUID] = []
    protected = 0
    for edge_id, from_id, to_id, edge_type, evidence in rows.tuples():
        if (from_id, to_id, edge_type) in computed:
            continue
        # protected_edge_types shields edges the SKIPPED semantic pass would have
        # reproduced — those carry no evidence pointer. A deterministic edge
        # (evidence set, e.g. cross-domain implements) is always recomputed when
        # its evidence still exists, so its absence here means the evidence is
        # genuinely gone: it must be deleted, never protected (invariant 7).
        if edge_type in protected_edge_types and evidence is None:
            protected += 1
            continue
        stale_ids.append(edge_id)
        logger.info(
            "event=linker_edge_deleted reason=evidence_gone edge_id=%s from=%s to=%s edge_type=%s",
            edge_id,
            from_id,
            to_id,
            edge_type,
        )
    if protected:
        logger.warning(
            "event=linker_stale_deletion_skipped reason=pass_skipped edge_types=%s count=%d",
            sorted(protected_edge_types),
            protected,
        )
    if not stale_ids:
        return 0
    await session.execute(delete(KnowledgeEdge).where(KnowledgeEdge.edge_id.in_(stale_ids)))
    return len(stale_ids)
