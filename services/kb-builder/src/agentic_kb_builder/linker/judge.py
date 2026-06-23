"""Phase-3B LLM relationship judge over bounded candidates.

The FIRST place the LLM rules on relationships. It reads ONLY the candidates the
cheap generator surfaced (relationship-candidates.md) — never a global sweep — and
for each pair asks the ModelClient for a verdict under the closed relation ontology
+ trust buckets. The verdict becomes a ``knowledge_edge`` row:

- ``INFERRED_HIGH`` / ``INFERRED_LOW`` -> an edge with ``source='llm_judge'``,
  ``trust_class`` = the bucket, ``evidence`` = the quoted-span pointer,
  ``relation_schema_version``, and ``valid_from_seq`` = this build's ``build_seq``
  (interval membership, so the broker actually serves it (as a labelled
  routing hint, never claim support).
- ``AMBIGUOUS`` -> an edge with ``trust_class='AMBIGUOUS'``; the broker excludes it
  from default traversal (trust-buckets.md), but it is retained for audit.
- ``REJECTED`` -> NO edge; retained in the judgment cache only.

Every model call is GATED by relationship_judgment_cache: a cache hit returns the
stored verdict and makes ZERO LLM calls. Idempotent on rebuild: judge edges upsert
on the (from, to, edge_type) partial unique index for source='llm_judge', and
cache rows on-conflict-do-nothing — a re-run accretes no duplicate edges or rows.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.domain import (
    JudgeCandidate,
    JudgeEndpoint,
    JudgeRelationType,
    JudgeTrustBucket,
    RelationshipJudgment,
)
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.domain.judge_records import INFERRED_EDGE_BUCKETS, guard_quote
from agentic_kb_builder.domain.schema_versions import (
    JUDGE_PROMPT_VERSION,
    RELATION_SCHEMA_VERSION,
)
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    KnowledgeEdge,
    RelationshipCandidate,
)
from agentic_kb_builder.linker.judgment_cache import (
    RelationshipJudgmentCacheGate,
    relationship_judgment_cache_parts,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

EDGE_SOURCE = "llm_judge"


class RelationshipJudge(Protocol):
    """The judge seam: the build depends on this, never on the SDK (rule python.md).
    Implemented by ChatModelClient; faked hermetically in tests."""

    @property
    def model_name(self) -> str: ...

    @property
    def model_params_hash(self) -> str: ...

    async def generate_relationship_judgment(
        self, *, candidate: JudgeCandidate, prompt_version: str
    ) -> RelationshipJudgment: ...


@dataclass(frozen=True)
class JudgeStats:
    candidates: int
    judged: int  # candidate pairs sent to the model (cache MISSES)
    cache_hits: int
    inferred_edges: int
    ambiguous_edges: int
    rejected: int


async def run_judge(
    session: AsyncSession,
    *,
    kb_version: str,
    valid_from_seq: int,
    judge: RelationshipJudge,
) -> JudgeStats:
    """Judge this build's candidates and reconcile the judge edges; return stats.

    valid_from_seq stamps newly inserted judge edges (interval membership); a
    refreshed edge keeps its original valid_from_seq (immutability,."""
    gate = RelationshipJudgmentCacheGate(session)
    candidates = await _load_candidates(session, kb_version=kb_version)
    model_version = judge.model_name + "|" + judge.model_params_hash

    judged = 0
    cache_hits = 0
    inferred = 0
    ambiguous = 0
    rejected = 0
    # logical judge links computed this run, so the reconcile pass can invalidate
    # any prior live judge edge whose pair is no longer inferred.
    computed: set[tuple[uuid.UUID, uuid.UUID, str]] = set()

    for cand in candidates:
        key = relationship_judgment_cache_parts(
            hash_a=cand.from_endpoint.content_hash,
            hash_b=cand.to_endpoint.content_hash,
            relation_schema_version=RELATION_SCHEMA_VERSION,
            prompt_version=JUDGE_PROMPT_VERSION,
            model_version=model_version,
        )
        hit = await gate.lookup(key)
        if hit is not None:
            cache_hits += 1
            # cached rows are written only from validated judgments, so the stored
            # relation/bucket are always in the Literal vocabularies.
            judgment = RelationshipJudgment(
                relation_type=cast("JudgeRelationType", hit.relation_type),
                trust_bucket=cast("JudgeTrustBucket", hit.trust_bucket),
                supporting_quote=hit.supporting_quote,
                reason=hit.reason,
            )
        else:
            judgment = await judge.generate_relationship_judgment(
                candidate=cand, prompt_version=JUDGE_PROMPT_VERSION
            )
            judged += 1
            await gate.record(key, judgment=judgment)

        # Defense-in-depth (invariant 7): re-guard at the edge-writing boundary, so a
        # RelationshipJudge impl that forgot to quote-guard — or a corrupt cache row —
        # can never write an INFERRED edge whose quote isn't a verbatim source span.
        judgment = guard_quote(judgment, cited_spans=cand.cited_spans)
        bucket = judgment.trust_bucket
        if bucket == "REJECTED":
            # Never an edge — retained in the cache only (audit).
            rejected += 1
            logger.info(
                "event=judge_rejected from=%s to=%s relation=%s",
                cand.from_endpoint.artifact_id,
                cand.to_endpoint.artifact_id,
                judgment.relation_type,
            )
            continue

        await _upsert_judge_edge(
            session,
            from_id=cand.from_endpoint.artifact_id,
            to_id=cand.to_endpoint.artifact_id,
            judgment=judgment,
            kb_version=kb_version,
            valid_from_seq=valid_from_seq,
        )
        computed.add(
            (cand.from_endpoint.artifact_id, cand.to_endpoint.artifact_id, judgment.relation_type)
        )
        if bucket in INFERRED_EDGE_BUCKETS:
            inferred += 1
        else:  # AMBIGUOUS — written but excluded from default traversal by the broker.
            ambiguous += 1
        logger.info(
            "event=judge_edge_written from=%s to=%s relation=%s trust_class=%s "
            "valid_from_seq=%d source=%s",
            cand.from_endpoint.artifact_id,
            cand.to_endpoint.artifact_id,
            judgment.relation_type,
            bucket,
            valid_from_seq,
            EDGE_SOURCE,
        )

    # Safety: never run the stale-sweep on an empty candidate set. A transient
    # candidate-load failure would otherwise leave `computed` empty and soft-invalidate
    # the ENTIRE live judge subgraph. Genuinely-removed endpoints are already
    # invalidated by the deletion sweep (invalidation.py), so skipping here is safe.
    if candidates:
        invalidated = await _invalidate_stale(session, computed, invalidated_at_seq=valid_from_seq)
    else:
        invalidated = 0
        logger.warning(
            "event=judge_stale_sweep_skipped reason=no_candidates kb_version=%s", kb_version
        )
    await session.flush()
    logger.info(
        "event=judge_completed kb_version=%s candidates=%d judged=%d cache_hits=%d "
        "inferred_edges=%d ambiguous_edges=%d rejected=%d invalidated=%d",
        kb_version,
        len(candidates),
        judged,
        cache_hits,
        inferred,
        ambiguous,
        rejected,
        invalidated,
    )
    return JudgeStats(
        candidates=len(candidates),
        judged=judged,
        cache_hits=cache_hits,
        inferred_edges=inferred,
        ambiguous_edges=ambiguous,
        rejected=rejected,
    )


async def _load_candidates(session: AsyncSession, *, kb_version: str) -> list[JudgeCandidate]:
    """Load this build's candidates joined with both endpoints' title + evidence.

    The judge sees ONLY the candidate pair's spans (no global context). A pair
    whose endpoint body is missing is skipped (nothing to quote-guard against)."""
    from_artifact = KnowledgeArtifact.__table__.alias("from_a")
    to_artifact = KnowledgeArtifact.__table__.alias("to_a")
    rows = await session.execute(
        select(
            RelationshipCandidate.from_artifact_id,
            from_artifact.c.title,
            from_artifact.c.body_text,
            RelationshipCandidate.to_artifact_id,
            to_artifact.c.title,
            to_artifact.c.body_text,
        )
        .join(from_artifact, RelationshipCandidate.from_artifact_id == from_artifact.c.artifact_id)
        .join(to_artifact, RelationshipCandidate.to_artifact_id == to_artifact.c.artifact_id)
        .where(RelationshipCandidate.kb_version == kb_version)
        # deterministic order so the (cache-gated) judging is reproducible.
        .order_by(RelationshipCandidate.from_artifact_id, RelationshipCandidate.to_artifact_id)
    )
    out: list[JudgeCandidate] = []
    for from_id, from_title, from_body, to_id, to_title, to_body in rows.tuples():
        if not from_body or not to_body:
            logger.info(
                "event=judge_candidate_skipped reason=no_evidence from=%s to=%s", from_id, to_id
            )
            continue
        out.append(
            JudgeCandidate(
                from_endpoint=JudgeEndpoint(
                    artifact_id=from_id,
                    title=from_title or str(from_id),
                    evidence_text=from_body,
                    content_hash=content_hash(from_body),
                ),
                to_endpoint=JudgeEndpoint(
                    artifact_id=to_id,
                    title=to_title or str(to_id),
                    evidence_text=to_body,
                    content_hash=content_hash(to_body),
                ),
            )
        )
    return out


def _evidence_pointer(judgment: RelationshipJudgment) -> dict[str, str]:
    """The quoted-span pointer stored on the edge (relation-ontology.md required
    fields). The verbatim quote IS the evidence for an inferred edge."""
    return {"quote": judgment.supporting_quote, "judge_prompt_version": JUDGE_PROMPT_VERSION}


async def _upsert_judge_edge(
    session: AsyncSession,
    *,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    judgment: RelationshipJudgment,
    kb_version: str,
    valid_from_seq: int,
) -> None:
    """Idempotent upsert on uq_knowledge_edge_judge (from, to, edge_type WHERE
    source='llm_judge'). On conflict refresh the trust bucket / evidence / label
    but KEEP the original valid_from_seq (membership immutability) and revive the
    row (invalidated_at_seq -> NULL)."""
    statement = (
        insert(KnowledgeEdge)
        .values(
            from_artifact_id=from_id,
            to_artifact_id=to_id,
            edge_type=judgment.relation_type,
            confidence=None,
            source=EDGE_SOURCE,
            kb_version=kb_version,
            valid_from_seq=valid_from_seq,
            trust_class=judgment.trust_bucket,
            relation_schema_version=RELATION_SCHEMA_VERSION,
            evidence=_evidence_pointer(judgment),
        )
        .on_conflict_do_update(
            index_elements=["from_artifact_id", "to_artifact_id", "edge_type"],
            index_where=text("source = 'llm_judge'"),
            set_={
                "trust_class": judgment.trust_bucket,
                "kb_version": kb_version,
                "relation_schema_version": RELATION_SCHEMA_VERSION,
                "evidence": _evidence_pointer(judgment),
                "invalidated_at_seq": None,
            },
        )
    )
    await session.execute(statement)


async def _invalidate_stale(
    session: AsyncSession,
    computed: set[tuple[uuid.UUID, uuid.UUID, str]],
    *,
    invalidated_at_seq: int,
) -> int:
    """Soft-invalidate live judge edges no longer produced this run (a candidate
    dropped, or a pair re-judged REJECTED). Sets invalidated_at_seq rather than
    deleting: the edge leaves the active version but stays a member
    of every prior version.

    Scans all live llm_judge edges irrespective of kb_version — correct under the
    single-writer nightly build (one build mutates the registry at a time). The
    caller guards against an empty computed set so a load failure can't wipe the
    subgraph.
    """
    rows = await session.execute(
        select(
            KnowledgeEdge.edge_id,
            KnowledgeEdge.from_artifact_id,
            KnowledgeEdge.to_artifact_id,
            KnowledgeEdge.edge_type,
        ).where(
            KnowledgeEdge.source == EDGE_SOURCE,
            KnowledgeEdge.invalidated_at_seq.is_(None),
        )
    )
    stale: list[uuid.UUID] = []
    for edge_id, from_id, to_id, edge_type in rows.tuples():
        if (from_id, to_id, edge_type) in computed:
            continue
        stale.append(edge_id)
        logger.info(
            "event=judge_edge_invalidated edge_id=%s from=%s to=%s edge_type=%s "
            "invalidated_at_seq=%d",
            edge_id,
            from_id,
            to_id,
            edge_type,
            invalidated_at_seq,
        )
    if not stale:
        return 0
    await session.execute(
        update(KnowledgeEdge)
        .where(KnowledgeEdge.edge_id.in_(stale))
        .values(invalidated_at_seq=invalidated_at_seq)
    )
    return len(stale)


__all__ = ["EDGE_SOURCE", "JudgeStats", "RelationshipJudge", "run_judge"]
