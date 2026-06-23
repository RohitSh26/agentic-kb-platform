"""Candidate-generator orchestration.

Loads the linkable artifacts + the live deterministic linker edges, runs the cheap
candidate generator, and writes the result to relationship_candidate ONLY. It does
NOT touch knowledge_edge and calls NO LLM (docs/contracts/relationship-candidates.md).

Idempotent: candidates upsert on (from_artifact_id, to_artifact_id, kb_version), so a
re-run of the same build re-writes the same candidate in place instead of accreting
duplicate rows. Candidates whose pair is no longer produced this build (for the same
kb_version) are pruned, so the audit set tracks the current generator output exactly.
"""

import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeEdge,
    RelationshipCandidate,
)
from agentic_kb_builder.linker.candidates import generate_candidates
from agentic_kb_builder.linker.run import _load_linkable_artifacts
from agentic_kb_builder.linker.semantic import SimilarityProvider
from agentic_kb_builder.linker.write_edges import EDGE_SOURCE
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


async def run_candidate_generator(
    session: AsyncSession,
    *,
    kb_version: str,
    similarity: SimilarityProvider | None = None,
) -> tuple[int, int, int]:
    """Generate cross-domain candidates and persist them to relationship_candidate.

    Returns (inserted, refreshed, pruned). NEVER writes to knowledge_edge.
    """
    artifacts = await _load_linkable_artifacts(session)
    existing_pairs = await _live_linker_pairs(session)
    drafts = await generate_candidates(
        artifacts, existing_pairs=existing_pairs, similarity=similarity
    )

    inserted = 0
    refreshed = 0
    produced: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for draft in drafts:
        produced.add((draft.from_artifact_id, draft.to_artifact_id))
        statement = (
            insert(RelationshipCandidate)
            .values(
                from_artifact_id=draft.from_artifact_id,
                to_artifact_id=draft.to_artifact_id,
                signals=draft.signals,
                candidate_recall_bucket=draft.candidate_recall_bucket,
                kb_version=kb_version,
            )
            .on_conflict_do_update(
                index_elements=["from_artifact_id", "to_artifact_id", "kb_version"],
                set_={
                    "signals": draft.signals,
                    "candidate_recall_bucket": draft.candidate_recall_bucket,
                },
            )
            .returning(text("(xmax = 0)"))
        )
        was_insert = (await session.execute(statement)).scalar_one()
        if was_insert:
            inserted += 1
        else:
            refreshed += 1

    pruned = await _prune_stale(session, kb_version=kb_version, produced=produced)
    await session.flush()
    logger.info(
        "event=candidate_written kb_version=%s inserted=%d refreshed=%d pruned=%d",
        kb_version,
        inserted,
        refreshed,
        pruned,
    )
    return inserted, refreshed, pruned


async def _live_linker_pairs(
    session: AsyncSession,
) -> frozenset[frozenset[uuid.UUID]]:
    """Unordered pairs already linked deterministically (live linker edges).

    A candidate whose pair is already a deterministic fact is excluded — the judge
    never re-judges it. Only LIVE edges count (invalidated_at_seq IS NULL).
    """
    rows = await session.execute(
        select(KnowledgeEdge.from_artifact_id, KnowledgeEdge.to_artifact_id).where(
            KnowledgeEdge.source == EDGE_SOURCE,
            KnowledgeEdge.invalidated_at_seq.is_(None),
        )
    )
    return frozenset(frozenset((frm, to)) for frm, to in rows.tuples())


async def _prune_stale(
    session: AsyncSession,
    *,
    kb_version: str,
    produced: set[tuple[uuid.UUID, uuid.UUID]],
) -> int:
    """Delete candidates for this kb_version that the generator no longer produces.

    This is an audit table (no membership / soft-delete semantics) so a stale
    candidate is physically removed: the set must reflect the current generator
    output exactly for the recall/volume metrics to be meaningful.
    """
    rows = await session.execute(
        select(
            RelationshipCandidate.candidate_id,
            RelationshipCandidate.from_artifact_id,
            RelationshipCandidate.to_artifact_id,
        ).where(RelationshipCandidate.kb_version == kb_version)
    )
    stale_ids = [
        candidate_id for candidate_id, frm, to in rows.tuples() if (frm, to) not in produced
    ]
    if not stale_ids:
        return 0
    await session.execute(
        delete(RelationshipCandidate).where(RelationshipCandidate.candidate_id.in_(stale_ids))
    )
    return len(stale_ids)


__all__ = ["run_candidate_generator"]
