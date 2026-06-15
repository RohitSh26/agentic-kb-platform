"""Golden-query case shape + evidence-recall metrics (docs/contracts/golden-query-evals.md).

The judge's headline risk for Design A is UNDERLINKING: the system returns
*real* citations and looks successful while silently missing the one
ADR/card/symbol that actually answers the question. Happy-path retrieval tests
do not catch this. The defence is a golden-query set with EXPECTED evidence,
scored by evidence-recall, used as a publish gate (publish-gates.md).

This module is the pure, DB-free seam: a GoldenCase shape + a GoldenResult
(what the broker actually returned for a case) + the metric functions over them.
A golden case generalises the existing missing_context_rate
(evidence_recall ~= 1 - missing_context_rate on the golden subset). Wiring the
broker run that produces GoldenResults is the harness's job (run.py); the metrics
here are unit-tested without a database.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# intent drives phase-4 temporal weighting; recorded from phase 0 so cases are
# stable. Closed set per the contract (extensible by bumping this tuple).
GoldenIntent = Literal[
    "how_does_x_work",
    "why_was_x_changed",
    "who_owns_x",
    "what_calls_x",
]

# the per-case evidence-recall floor when a case does not set min_evidence_recall;
# the gate's authoritative floor is publish-gates.md (>= 0.95).
DEFAULT_MIN_EVIDENCE_RECALL = 0.95


class GoldenCase(BaseModel):
    """One golden query with its expected evidence (recall numerator) and the
    ACL/relation expectations the publish gate scores."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9/-]{1,96}$")
    query: str = Field(min_length=1)
    intent: GoldenIntent
    requester_teams: list[str] = Field(default_factory=list)
    # evidence ids that MUST appear in the broker result (recall numerator).
    expected_evidence_ids: list[str] = Field(min_length=1)
    # optional: relations that must be discoverable, per edge_type.
    expected_edge_types: list[str] = Field(default_factory=list)
    # optional ACL negative — these ids must NEVER appear.
    must_not_leak_ids: list[str] = Field(default_factory=list)
    min_evidence_recall: float = Field(default=DEFAULT_MIN_EVIDENCE_RECALL, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _expected_and_leak_disjoint(self) -> Self:
        overlap = set(self.expected_evidence_ids) & set(self.must_not_leak_ids)
        if overlap:
            raise ValueError(
                f"{self.case_id}: ids both expected and must-not-leak: {sorted(overlap)}"
            )
        return self


# The coarse source kinds PR-33 derives in the broker (context_broker/temporal.py).
# Duplicated, not imported, across the service boundary (ADR-0008).
OrderedKind = Literal["code", "doc", "card", "pr", "adr", "other"]

# Per-intent ordering expectation (PR-33). `lead_kinds` = the kind(s) allowed as
# PRIMARY (index 0); `include_any` = if non-empty, at least one of these kinds must
# appear somewhere in the result (the change trail for "why"). Mirrors the broker's
# intent weighting so a golden case can assert it actually reordered as promised.
_INTENT_ORDERING: dict[GoldenIntent, tuple[frozenset[OrderedKind], frozenset[OrderedKind]]] = {
    "how_does_x_work": (frozenset({"code"}), frozenset()),
    "what_calls_x": (frozenset({"code"}), frozenset()),
    "why_was_x_changed": (frozenset({"card", "pr", "adr"}), frozenset({"card", "pr", "adr"})),
    "who_owns_x": (frozenset({"pr", "card", "code"}), frozenset()),
}


@dataclass(frozen=True)
class GoldenResult:
    """What the broker returned for one golden case — the DB-free input to the
    metrics. returned_evidence_ids is everything the case surfaced (cards +
    expansions); surfaced_edges maps edge_type -> the (from,to) pairs the graph
    tools returned for the case."""

    case: GoldenCase
    returned_evidence_ids: frozenset[str]
    surfaced_edges: dict[str, frozenset[tuple[str, str]]] = field(
        default_factory=dict[str, frozenset[tuple[str, str]]]
    )
    # expected (from,to) pairs per edge_type, when the case asserts specific
    # relations (phase-2 enforcing; phase-1 reporting only).
    expected_edges: dict[str, frozenset[tuple[str, str]]] = field(
        default_factory=dict[str, frozenset[tuple[str, str]]]
    )
    # PR-33: the ordered source kinds the broker returned (index 0 = primary
    # evidence), and the ids it flagged PR-33-stale. Empty ⇒ the case does not
    # assert ordering (phase-0/1 recall-only cases stay unchanged).
    ordered_kinds: tuple[OrderedKind, ...] = ()
    stale_primary: bool = False


def evidence_recall(result: GoldenResult) -> float:
    """|returned ∩ expected| / |expected| for one case. The contract's first-class
    metric; the golden-set average gates the publish (>= 0.95)."""
    expected = set(result.case.expected_evidence_ids)
    if not expected:
        return 1.0
    found = expected & result.returned_evidence_ids
    return len(found) / len(expected)


def acl_leak_count(result: GoldenResult) -> int:
    """Number of must_not_leak_ids that appeared. MUST be 0 (hard gate)."""
    return len(set(result.case.must_not_leak_ids) & result.returned_evidence_ids)


def intent_ordering_ok(result: GoldenResult) -> bool | None:
    """PR-33: did the broker order evidence as the case's intent requires?

    Returns None when the case asserts no ordering (no ordered_kinds), so
    recall-only golden cases are unaffected. Otherwise True iff:
      * the PRIMARY (index 0) returned kind is one of the intent's lead kinds
        (current code first for `how`/`what_calls`; a card/PR/ADR for `why`),
      * at least one history kind (card/PR/ADR) is present for `why`, and
      * no PR-33-stale doc was returned as primary evidence.
    Pure + deterministic — the broker computed the order; this only checks it.
    """
    if not result.ordered_kinds:
        return None
    lead_kinds, include_any = _INTENT_ORDERING[result.case.intent]
    primary = result.ordered_kinds[0]
    if primary not in lead_kinds:
        return False
    if result.stale_primary:
        return False
    present = set(result.ordered_kinds)
    # `include_any` non-empty ⇒ at least one of those kinds must be present (the
    # change trail for "why"). Empty ⇒ no extra inclusion requirement.
    return not include_any or bool(include_any & present)


def missing_expected(result: GoldenResult) -> tuple[str, ...]:
    """Expected evidence ids that did NOT appear — the underlinking failures."""
    return tuple(sorted(set(result.case.expected_evidence_ids) - result.returned_evidence_ids))


@dataclass(frozen=True)
class EdgeScore:
    """Per-edge-type precision/recall (publish-gates.md phase-2 ENFORCING gate;
    phase-1 REPORTING only). precision = correct surfaced / surfaced;
    recall = expected found / expected. None when there is nothing to score."""

    edge_type: str
    precision: float | None
    recall: float | None


def edge_scores(result: GoldenResult) -> tuple[EdgeScore, ...]:
    """Per-edge-type precision/recall over the case's surfaced vs expected edges.

    Phase 1: REPORTED, not enforced (relation precision is a phase-2 ENFORCING
    gate, skipped in phase 1 per publish-gates.md). Cheap to compute from the two
    edge maps, so we report it now to make a weak relation visible rather than
    leaving a TODO.
    """
    edge_types = sorted(set(result.surfaced_edges) | set(result.expected_edges))
    scores: list[EdgeScore] = []
    for edge_type in edge_types:
        surfaced = result.surfaced_edges.get(edge_type, frozenset())
        expected = result.expected_edges.get(edge_type, frozenset())
        correct = surfaced & expected
        precision = (len(correct) / len(surfaced)) if surfaced else None
        recall = (len(correct) / len(expected)) if expected else None
        scores.append(EdgeScore(edge_type=edge_type, precision=precision, recall=recall))
    return tuple(scores)


@dataclass(frozen=True)
class GoldenReport:
    """Aggregate golden-set metrics — the publish-gate inputs (publish-gates.md)."""

    cases: int
    mean_evidence_recall: float | None
    min_evidence_recall: float | None
    total_acl_leaks: int
    cases_below_floor: tuple[str, ...]
    edge_precision: dict[str, float | None]
    edge_recall: dict[str, float | None]
    # PR-33: case_ids whose returned ordering did NOT satisfy their intent (a card
    # not surfaced for "why", a stale doc primary for "how", etc.). Empty unless a
    # case asserts ordering; a non-empty set is a temporal-semantics failure.
    intent_ordering_failures: tuple[str, ...] = ()


def aggregate(results: list[GoldenResult]) -> GoldenReport:
    """Fold per-case scores into the golden-set report. cases_below_floor lists
    cases whose evidence_recall fell under their min_evidence_recall — the publish
    gate fails when this is non-empty or any ACL leak occurred."""
    if not results:
        return GoldenReport(
            cases=0,
            mean_evidence_recall=None,
            min_evidence_recall=None,
            total_acl_leaks=0,
            cases_below_floor=(),
            edge_precision={},
            edge_recall={},
            intent_ordering_failures=(),
        )
    recalls = [evidence_recall(r) for r in results]
    below = tuple(
        r.case.case_id for r in results if evidence_recall(r) < r.case.min_evidence_recall
    )
    leaks = sum(acl_leak_count(r) for r in results)

    precision_sums: dict[str, list[float]] = {}
    recall_sums: dict[str, list[float]] = {}
    for result in results:
        for score in edge_scores(result):
            if score.precision is not None:
                precision_sums.setdefault(score.edge_type, []).append(score.precision)
            if score.recall is not None:
                recall_sums.setdefault(score.edge_type, []).append(score.recall)

    ordering_failures = tuple(r.case.case_id for r in results if intent_ordering_ok(r) is False)

    return GoldenReport(
        cases=len(results),
        mean_evidence_recall=sum(recalls) / len(recalls),
        min_evidence_recall=min(recalls),
        total_acl_leaks=leaks,
        cases_below_floor=below,
        edge_precision={k: sum(v) / len(v) for k, v in precision_sums.items()},
        edge_recall={k: sum(v) / len(v) for k, v in recall_sums.items()},
        intent_ordering_failures=ordering_failures,
    )


def load_golden_case(path: Path) -> GoldenCase:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return GoldenCase.model_validate(raw)


def load_golden_cases(directory: Path) -> list[GoldenCase]:
    """Load every *.yaml golden case in a directory (may be near-empty in phase 0)."""
    cases = [load_golden_case(path) for path in sorted(directory.glob("*.yaml"))]
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise ValueError(f"duplicate golden case_id: {case.case_id}")
        seen.add(case.case_id)
    return cases
