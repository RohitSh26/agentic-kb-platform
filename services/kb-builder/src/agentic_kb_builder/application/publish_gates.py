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

from sqlalchemy import func, select, update
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
    }
)

# Phase-1 gate thresholds (docs/contracts/publish-gates.md). Tunable; structural
# changes go through an ADR.
EXTRACTOR_ERROR_RATE_THRESHOLD = 0.01  # files failed AST extraction / files extracted
SYMBOL_COUNT_DELTA_THRESHOLD = 0.25  # |new - prev| / prev, override via allow_large_delta
EVIDENCE_RECALL_FLOOR = 0.95  # golden-query evidence_recall (proxy in phase 1)


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


async def _previous_active_kb_version(session: AsyncSession, kb_version: str) -> str | None:
    """The kb_version still serving (status active) — never the build under gate."""
    return (
        await session.execute(
            select(KbBuildRun.kb_version).where(
                KbBuildRun.status == "active", KbBuildRun.kb_version != kb_version
            )
        )
    ).scalar_one_or_none()


async def _count_artifacts(session: AsyncSession, kb_version: str, artifact_type: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeArtifact)
            .where(
                KnowledgeArtifact.kb_version == kb_version,
                KnowledgeArtifact.artifact_type == artifact_type,
            )
        )
    ).scalar_one()


async def extractor_error_rate_gate(session: AsyncSession, kb_version: str) -> GateResult:
    """files that failed AST extraction / files extracted <= threshold (default 1%).

    extractor_failures is recorded by the build runner on kb_build_run; the
    denominator is the code_file artifacts this build produced. No files extracted
    (e.g. a docs-only build) trivially passes — there is nothing to fail.
    """
    run = (
        await session.execute(
            select(KbBuildRun).where(
                KbBuildRun.kb_version == kb_version, KbBuildRun.status == "completed"
            )
        )
    ).scalar_one_or_none()
    failures = run.extractor_failures if run is not None else 0
    files = await _count_artifacts(session, kb_version, "code_file")
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
    run = (
        await session.execute(
            select(KbBuildRun).where(
                KbBuildRun.kb_version == kb_version, KbBuildRun.status == "completed"
            )
        )
    ).scalar_one_or_none()
    override = bool(run.allow_large_delta) if run is not None else False
    previous = await _previous_active_kb_version(session, kb_version)
    new_symbols = await _count_artifacts(session, kb_version, "code_symbol")
    if previous is None:
        logger.info(
            "event=publish_gate gate=symbol_count_delta kb_version=%s baseline=none passed=true",
            kb_version,
        )
        return GateResult(name="symbol_count_delta", passed=True, measured_value=0.0)
    prev_symbols = await _count_artifacts(session, previous, "code_symbol")
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
    dangling = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeArtifact)
            .outerjoin(SourceItem, KnowledgeArtifact.source_id == SourceItem.source_id)
            .where(
                KnowledgeArtifact.kb_version == kb_version,
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
    """Every knowledge_edge has an allowed edge_type AND both endpoints exist.

    Two integrity classes: an edge_type outside the closed ontology
    (relation-ontology.md), or an endpoint artifact missing from THIS kb_version
    (a ghost endpoint). Either fails the build. measured_value is the total
    offending edge count.
    """
    bad_type = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeEdge)
            .where(
                KnowledgeEdge.kb_version == kb_version,
                KnowledgeEdge.edge_type.notin_(sorted(ALLOWED_EDGE_TYPES)),
            )
        )
    ).scalar_one()
    valid_ids = (
        select(KnowledgeArtifact.artifact_id)
        .where(KnowledgeArtifact.kb_version == kb_version)
        .scalar_subquery()
    )
    dangling_endpoint = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeEdge)
            .where(
                KnowledgeEdge.kb_version == kb_version,
                (KnowledgeEdge.from_artifact_id.notin_(valid_ids))
                | (KnowledgeEdge.to_artifact_id.notin_(valid_ids)),
            )
        )
    ).scalar_one()
    offending = bad_type + dangling_endpoint
    passed = offending == 0
    log = logger.info if passed else logger.error
    log(
        "event=publish_gate gate=edge_evidence_integrity kb_version=%s bad_type=%d "
        "dangling_endpoint=%d passed=%s",
        kb_version,
        bad_type,
        dangling_endpoint,
        passed,
    )
    return GateResult(
        name="edge_evidence_integrity", passed=passed, measured_value=float(offending)
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
    symbols = await _count_artifacts(session, kb_version, "code_symbol")
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
                    KnowledgeArtifact.kb_version == kb_version,
                    KnowledgeArtifact.artifact_type == "code_symbol",
                    KnowledgeEdge.kb_version == kb_version,
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
    """The phase-1 publish gate, composing the existing index-consistency validator
    FIRST with the new registry gates. Index consistency is the existing gate
    (make_consistency_validator); the rest extend it. Phase-2 gates (relation
    precision, no-ghost-edges) are intentionally absent — skipped, not failed.
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
            evidence_recall_gate,
        ]
    )
