"""Publish gates that gate kb_version activation (docs/contracts/publish-gates.md).

"Active only after validation" (invariant 5) needs concrete, automated checks.
This module composes the phase-1 gates into one ValidationHook so a kb_version
activates ONLY when every applicable gate passes. The headline risk for Design A
is UNDERLINKING — real citations that silently miss the key symbol/ADR/card —
caught here by the evidence-integrity and (proxy) evidence-recall gates.

Composition: each gate is an async callable returning GateResult; the composed
hook runs them in order, and the FIRST failing, non-overridden gate records
which gate + its measured value on kb_build_run and returns False, so activation
never happens and MCP keeps serving the last active version (active_version.py
leaves the previous active row untouched on a False hook).

Phasing: only phase-1 gates are enforcing here. Phase-2 gates (relation
precision, no-ghost-edges) are SKIPPED, not failed (ADR-0010) — they are not
wired in until their producing mechanism exists; making them enforcing early
would block otherwise-valid builds.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.application.active_version import ValidationHook
from agentic_kb_builder.infrastructure.postgres.models import (
    KbBuildRun,
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Closed edge-type vocabulary (docs/contracts/relation-ontology.md) PLUS the
# producer names the deterministic mechanisms actually emit today: graphify uses
# "exposed_as" (the ontology's "exposes") and the linker uses "requests". Any
# edge_type outside this set is an ontology violation — the edge-evidence-integrity
# gate rejects the build rather than letting an unknown relation slip into production.
ALLOWED_EDGE_TYPES: frozenset[str] = frozenset(
    {
        # ontology
        "imports",
        "calls",
        "inherits",
        "exposes",
        "tests",
        "documents",
        "implements",
        "mentions",
        # producer aliases in use today
        "exposed_as",
        "requests",
        # structural code edges (ADR-0020): deterministic symbol->file / file->file
        "defined_in",
        # Graphify cross-file symbol relations (ADR-0012 delegation): a symbol uses /
        # type-references another, ingested whole-tree for symbol-level dependency.
        "uses",
        "references",
        # Alias index (PR-38 / ADR-0030): alias_reference -> target artifact, evidence =
        # the deterministic mining source (relation-ontology.md). Found missing by the
        # gate itself on the first full build after PR-38 landed (2026-07-03) — the
        # ontology contract had the row, this enforcement-side copy didn't.
        "aliases",
    }
)

# Phase-1 gate thresholds (docs/contracts/publish-gates.md). Tunable; structural
# changes go through an ADR.
EXTRACTOR_ERROR_RATE_THRESHOLD = 0.01  # files failed AST extraction / files extracted
SYMBOL_COUNT_DELTA_THRESHOLD = 0.25  # |new - prev| / prev, override via allow_large_delta
EVIDENCE_RECALL_FLOOR = 0.95  # golden-query evidence_recall (proxy in phase 1)


async def _completed_run(session: AsyncSession, kb_version: str) -> KbBuildRun | None:
    """The 'completed' kb_build_run under gate (activation has not run yet), or
    None. Gates that need both run fields (extractor_failures, allow_large_delta)
    and the build_seq read this once instead of re-querying for each.

    A retry can leave several 'completed' runs for one kb_version; take the LATEST
    by build_seq (matching _build_seq_for) so this never raises on retry and the
    run fields it returns belong to the same build the gates measure against."""
    return (
        await session.execute(
            select(KbBuildRun)
            .where(KbBuildRun.kb_version == kb_version, KbBuildRun.status == "completed")
            .order_by(KbBuildRun.build_seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _build_seq_for(session: AsyncSession, kb_version: str) -> int:
    """The build_seq of the build under gate (interval-membership cutoff S).

    The gates evaluate "the served set" of THIS build by membership against its
    own build_seq, not by kb_version label-equality (version-membership.md). The
    build under gate is 'completed' (activation has not run yet).

    A kb_version may be (re)built more than once — a retry leaves more than one
    'completed' kb_build_run with the same label — so we deterministically take
    the LATEST completed build_seq rather than asserting uniqueness (which would
    raise MultipleResultsFound on retry and crash activation).
    """
    seq = (
        await session.execute(
            select(KbBuildRun.build_seq)
            .where(KbBuildRun.kb_version == kb_version, KbBuildRun.status == "completed")
            .order_by(KbBuildRun.build_seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if seq is None:
        raise ValueError(
            f"no completed kb_build_run for kb_version={kb_version!r} under publish gate"
        )
    return seq


def _members(seq: int) -> ColumnElement[bool]:
    """Boolean clause: a knowledge_artifact row is a member of version `seq`."""
    return (KnowledgeArtifact.valid_from_seq <= seq) & (
        (KnowledgeArtifact.invalidated_at_seq.is_(None))
        | (KnowledgeArtifact.invalidated_at_seq > seq)
    )


def _edge_members(seq: int) -> ColumnElement[bool]:
    """Boolean clause: a knowledge_edge row is a member of version `seq`."""
    return (KnowledgeEdge.valid_from_seq <= seq) & (
        (KnowledgeEdge.invalidated_at_seq.is_(None)) | (KnowledgeEdge.invalidated_at_seq > seq)
    )


@dataclass(frozen=True)
class GateResult:
    """One gate's outcome. measured_value is the number recorded on kb_build_run
    when the gate FAILS (so a failed publish is queryable); skipped gates never
    fail and carry no measured value."""

    name: str
    passed: bool
    measured_value: float | None = None
    skipped: bool = False


# A gate reads the registry for one kb_version and reports pass/fail.
Gate = Callable[[AsyncSession, str], Awaitable[GateResult]]


async def _previous_active_seq(session: AsyncSession, kb_version: str) -> int | None:
    """The build_seq still serving (status active) — never the build under gate.

    Used as the membership cutoff for the previous version's served set (e.g. the
    symbol-count-delta baseline), so the comparison counts what MCP actually
    serves, not just rows the prior build labelled.
    """
    return (
        await session.execute(
            select(KbBuildRun.build_seq).where(
                KbBuildRun.status == "active", KbBuildRun.kb_version != kb_version
            )
        )
    ).scalar_one_or_none()


async def _count_artifacts(session: AsyncSession, seq: int, artifact_type: str) -> int:
    """Count artifacts of `artifact_type` that are MEMBERS of version `seq`
    (the served set), not merely rows labelled with this build's kb_version."""
    return (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeArtifact)
            .where(_members(seq), KnowledgeArtifact.artifact_type == artifact_type)
        )
    ).scalar_one()


async def extractor_error_rate_gate(session: AsyncSession, kb_version: str) -> GateResult:
    """files that failed AST extraction / files extracted <= threshold (default 1%).

    extractor_failures is recorded by the build runner on kb_build_run; the
    denominator is the code_file artifacts this build produced. No files extracted
    (e.g. a docs-only build) trivially passes — there is nothing to fail.
    """
    run = await _completed_run(session, kb_version)
    failures = run.extractor_failures if run is not None else 0
    seq = await _build_seq_for(session, kb_version)
    files = await _count_artifacts(session, seq, "code_file")
    attempted = files + failures
    rate = (failures / attempted) if attempted else 0.0
    passed = rate <= EXTRACTOR_ERROR_RATE_THRESHOLD
    logger.info(
        "event=publish_gate gate=extractor_error_rate kb_version=%s failures=%d "
        "attempted=%d rate=%.4f passed=%s",
        kb_version,
        failures,
        attempted,
        rate,
        passed,
    )
    return GateResult(name="extractor_error_rate", passed=passed, measured_value=rate)


async def symbol_count_delta_gate(session: AsyncSession, kb_version: str) -> GateResult:
    """|symbols(new) - symbols(prev)| / symbols(prev) <= threshold, unless override.

    The override (allow_large_delta on kb_build_run) is honoured here and logged;
    no previous active version (first build) trivially passes — there is no baseline.
    """
    run = await _completed_run(session, kb_version)
    override = bool(run.allow_large_delta) if run is not None else False
    seq = await _build_seq_for(session, kb_version)
    previous_seq = await _previous_active_seq(session, kb_version)
    new_symbols = await _count_artifacts(session, seq, "code_symbol")
    if previous_seq is None:
        logger.info(
            "event=publish_gate gate=symbol_count_delta kb_version=%s baseline=none passed=true",
            kb_version,
        )
        return GateResult(name="symbol_count_delta", passed=True, measured_value=0.0)
    prev_symbols = await _count_artifacts(session, previous_seq, "code_symbol")
    delta = abs(new_symbols - prev_symbols) / prev_symbols if prev_symbols else 0.0
    within = delta <= SYMBOL_COUNT_DELTA_THRESHOLD
    passed = within or override
    if not within and override:
        logger.warning(
            "event=publish_gate_override gate=symbol_count_delta kb_version=%s delta=%.4f "
            "threshold=%.4f reason=allow_large_delta",
            kb_version,
            delta,
            SYMBOL_COUNT_DELTA_THRESHOLD,
        )
    logger.info(
        "event=publish_gate gate=symbol_count_delta kb_version=%s prev=%d new=%d delta=%.4f "
        "override=%s passed=%s",
        kb_version,
        prev_symbols,
        new_symbols,
        delta,
        override,
        passed,
    )
    return GateResult(name="symbol_count_delta", passed=passed, measured_value=delta)


async def no_dangling_citations_gate(session: AsyncSession, kb_version: str) -> GateResult:
    """Every citeable artifact's evidence pointer resolves within the new version.

    An artifact's evidence pointer is its source_item (uri/version/span); a
    citation dangles if the backing source row is missing or marked deleted, so
    the cited fact can never be opened to its source span (the L2 path). Counts
    such artifacts; any > 0 fails. measured_value is the dangling count.
    """
    seq = await _build_seq_for(session, kb_version)
    dangling = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeArtifact)
            .outerjoin(SourceItem, KnowledgeArtifact.source_id == SourceItem.source_id)
            .where(
                _members(seq),
                KnowledgeArtifact.body_text.is_not(None),
                (SourceItem.source_id.is_(None)) | (SourceItem.is_deleted.is_(True)),
            )
        )
    ).scalar_one()
    passed = dangling == 0
    log = logger.info if passed else logger.error
    log(
        "event=publish_gate gate=no_dangling_citations kb_version=%s dangling=%d passed=%s",
        kb_version,
        dangling,
        passed,
    )
    return GateResult(name="no_dangling_citations", passed=passed, measured_value=float(dangling))


async def edge_evidence_integrity_gate(session: AsyncSession, kb_version: str) -> GateResult:
    """Every edge that is a MEMBER of this build has an allowed edge_type AND both
    endpoints are MEMBERS of this build (no-ghost-edges, PR-27 / ADR-0013).

    This is the enforcing no-ghost-edges gate. Scoped by interval membership, not
    kb_version label-equality (version-membership.md): a legitimate cross-version
    edge (introduced earlier, still live) passes because its endpoints are members
    of the served set; an edge to an invalidated/absent artifact fails. Two
    integrity classes: an edge_type outside the closed ontology
    (relation-ontology.md), or a ghost endpoint (an endpoint not a member of this
    build). Either fails the build. measured_value is the total offending count.
    """
    seq = await _build_seq_for(session, kb_version)
    bad_type = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeEdge)
            .where(
                _edge_members(seq),
                KnowledgeEdge.edge_type.notin_(sorted(ALLOWED_EDGE_TYPES)),
            )
        )
    ).scalar_one()
    member_ids = select(KnowledgeArtifact.artifact_id).where(_members(seq)).scalar_subquery()
    dangling_endpoint = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeEdge)
            .where(
                _edge_members(seq),
                (KnowledgeEdge.from_artifact_id.notin_(member_ids))
                | (KnowledgeEdge.to_artifact_id.notin_(member_ids)),
            )
        )
    ).scalar_one()
    offending = bad_type + dangling_endpoint
    passed = offending == 0
    log = logger.info if passed else logger.error
    log(
        "event=publish_gate gate=no_ghost_edges kb_version=%s seq=%d bad_type=%d "
        "ghost_endpoint=%d passed=%s",
        kb_version,
        seq,
        bad_type,
        dangling_endpoint,
        passed,
    )
    return GateResult(
        name="edge_evidence_integrity", passed=passed, measured_value=float(offending)
    )


async def relation_precision_gate(session: AsyncSession, kb_version: str) -> GateResult:
    """Per-edge_type relation precision >= 0.9 for relations in production (PR-27).

    Split into two parts (publish-gates.md, ADR-0013):

    - ENFORCING here: the registry-derivable integrity of the served edge set —
      every edge that is a MEMBER of this build and was produced by the
      deterministic linker (source='linker', the cross-domain relations in
      production) MUST carry an evidence pointer (relation-ontology.md "Required
      edge fields": an edge without a valid evidence pointer MUST NOT be written).
      A member linker edge with NULL evidence is a precision violation that the
      registry can prove without the golden set, so it blocks activation.
    - SEAM (logged, non-blocking): the authoritative per-edge_type precision over a
      labelled golden set is computed by the evals harness (make eval-run), the
      same seam as evidence_recall — kb-builder cannot import evals/ (ADR-0008).

    measured_value is the count of member linker edges missing an evidence pointer.
    """
    seq = await _build_seq_for(session, kb_version)
    missing_evidence = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeEdge)
            .where(
                _edge_members(seq),
                KnowledgeEdge.source == "linker",
                KnowledgeEdge.evidence.is_(None),
            )
        )
    ).scalar_one()
    passed = missing_evidence == 0
    log = logger.info if passed else logger.error
    log(
        "event=publish_gate gate=relation_precision kb_version=%s seq=%d "
        "member_linker_edges_missing_evidence=%d seam=evals_harness passed=%s",
        kb_version,
        seq,
        missing_evidence,
        passed,
    )
    return GateResult(
        name="relation_precision", passed=passed, measured_value=float(missing_evidence)
    )


async def evidence_recall_gate(session: AsyncSession, kb_version: str) -> GateResult:
    """evidence_recall >= 0.95 + acl_leak_count == 0 over the golden set.

    SEAM (documented, intentionally NON-ENFORCING in activation). The authoritative
    evidence_recall is computed by the evals harness
    (docs/contracts/golden-query-evals.md), which runs the FULL Context Broker
    (retrieval + ACL + budget) against the registry over the golden query set —
    far too heavy to spin up inside activation, and the golden set lives in
    `evals/` (a root project) which kb-builder MUST NOT import (service boundary,
    ADR-0008). So in phase 1 this gate runs in CI as `make eval-run`, not here.

    We still COMPUTE and LOG a registry-derivable PROXY (linked_symbols /
    total_symbols) so underlinking is observable from build logs, but the gate is
    SKIPPED — it never blocks activation. A graph-coverage proxy would otherwise
    reject legitimate builds with leaf symbols that have no incident edge. The
    real recall gate becomes enforcing in the harness as the golden set grows
    (phase 2), wired through this same seam without a schema change.
    """
    seq = await _build_seq_for(session, kb_version)
    symbols = await _count_artifacts(session, seq, "code_symbol")
    if symbols:
        linked = (
            await session.execute(
                select(func.count(func.distinct(KnowledgeArtifact.artifact_id)))
                .select_from(KnowledgeArtifact)
                .join(
                    KnowledgeEdge,
                    (KnowledgeEdge.from_artifact_id == KnowledgeArtifact.artifact_id)
                    | (KnowledgeEdge.to_artifact_id == KnowledgeArtifact.artifact_id),
                )
                .where(
                    _members(seq),
                    KnowledgeArtifact.artifact_type == "code_symbol",
                    _edge_members(seq),
                )
            )
        ).scalar_one()
        logger.info(
            "event=publish_gate gate=evidence_recall kb_version=%s seam=evals_harness "
            "proxy_linked=%d proxy_symbols=%d proxy_recall=%.4f floor=%.2f skipped=true",
            kb_version,
            linked,
            symbols,
            linked / symbols,
            EVIDENCE_RECALL_FLOOR,
        )
    else:
        logger.info(
            "event=publish_gate gate=evidence_recall kb_version=%s seam=evals_harness "
            "symbols=0 skipped=true",
            kb_version,
        )
    return GateResult(name="evidence_recall", passed=True, skipped=True)


async def _record_failure(session: AsyncSession, kb_version: str, result: GateResult) -> None:
    await session.execute(
        update(KbBuildRun)
        .where(KbBuildRun.kb_version == kb_version, KbBuildRun.status == "completed")
        .values(failed_gate=result.name, gate_measured_value=result.measured_value)
    )
    logger.error(
        "event=publish_gate_failed kb_version=%s gate=%s measured_value=%s "
        "active_version_unchanged=true",
        kb_version,
        result.name,
        result.measured_value,
    )


def compose_gates(gates: list[Gate]) -> ValidationHook:
    """Fold phase-1 gates into one ValidationHook (active_version.py shape).

    Runs each gate in order; the first non-skipped failure records the gate +
    measured value on kb_build_run and returns False (activation never happens,
    previous active version keeps serving). All-pass returns True.
    """

    async def _validate(session: AsyncSession, kb_version: str) -> bool:
        for gate in gates:
            result = await gate(session, kb_version)
            if result.skipped:
                logger.info(
                    "event=publish_gate_skipped kb_version=%s gate=%s", kb_version, result.name
                )
                continue
            if not result.passed:
                await _record_failure(session, kb_version, result)
                await session.flush()
                return False
        logger.info("event=publish_gates_passed kb_version=%s", kb_version)
        return True

    return _validate


def make_publish_gate_validator(consistency: ValidationHook) -> ValidationHook:
    """The publish gate, composing the existing index-consistency validator FIRST
    with the registry gates. Index consistency is the existing gate
    (make_consistency_validator); the rest extend it. Phase-2 gates are now
    ENFORCING (PR-27 / ADR-0013): no-ghost-edges (edge_evidence_integrity_gate,
    membership-scoped) and the registry-derivable part of relation precision
    (relation_precision_gate). evidence_recall stays on the evals-harness seam.
    """

    async def index_consistency_gate(session: AsyncSession, kb_version: str) -> GateResult:
        return GateResult(name="index_consistency", passed=await consistency(session, kb_version))

    return compose_gates(
        [
            index_consistency_gate,
            extractor_error_rate_gate,
            symbol_count_delta_gate,
            no_dangling_citations_gate,
            edge_evidence_integrity_gate,
            relation_precision_gate,
            evidence_recall_gate,
        ]
    )
