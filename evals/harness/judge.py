"""LLM-relationship-judge quality metrics (PR-29, docs/contracts/relationship-judgment.md).

Phase 3B promotes a bounded candidate subset to INFERRED_* edges. Two numbers decide
whether judging earns its tokens:

- **inferred_edge_precision** — of the INFERRED_* edges the judge wrote, the fraction a
  reviewer judges real. Must stay at/above the gate (an inferred edge is a routing hint, so
  a wrong one wastes a hop but never corrupts a claim — yet precision must not collapse).
- **cross-domain evidence-recall LIFT** — how much the INFERRED edges raise cross-domain
  evidence-recall OVER the deterministic-only baseline. This is the whole point of phase 3B:
  inferred routing hints should reach real cross-domain evidence the deterministic linker
  missed, WITHOUT dropping precision below the gate.

This module is the pure, DB-free seam (ADR-0008: evals cannot import kb-builder). It scores a
fixed judged-edge set; the authoritative DB-backed behaviour lives in kb-builder's
tests/integration/test_judge.py.
"""

from dataclasses import dataclass

# An INFERRED edge is a routing hint, never claim support, so the precision gate is lower
# than for EXTRACTED facts — but a judge below this is not worth its tokens.
INFERRED_PRECISION_GATE = 0.6
# Phase 3B must add cross-domain reach, not just churn: require a positive recall lift.
MIN_RECALL_LIFT = 0.0

INFERRED_BUCKETS: frozenset[str] = frozenset({"INFERRED_HIGH", "INFERRED_LOW"})


@dataclass(frozen=True)
class JudgedEdge:
    """One judge verdict over a candidate pair: the unordered endpoint-key pair, the bucket
    the judge assigned, and whether a sampled reviewer judged the relationship real."""

    from_key: str
    to_key: str
    trust_bucket: str
    is_relevant: bool | None = None

    @property
    def unordered(self) -> frozenset[str]:
        return frozenset((self.from_key, self.to_key))

    @property
    def is_inferred_edge(self) -> bool:
        return self.trust_bucket in INFERRED_BUCKETS


@dataclass(frozen=True)
class JudgeReport:
    inferred_edges: int
    inferred_edge_precision: float | None
    deterministic_recall: float | None
    combined_recall: float | None
    recall_lift: float | None


def inferred_edge_precision(edges: list[JudgedEdge]) -> float | None:
    """Sampled precision over the INFERRED_* edges the judge wrote (AMBIGUOUS/REJECTED never
    become edges, so they are excluded). None when nothing was sampled."""
    sampled = [e for e in edges if e.is_inferred_edge and e.is_relevant is not None]
    if not sampled:
        return None
    return sum(1 for e in sampled if e.is_relevant) / len(sampled)


def _recall(found_pairs: set[frozenset[str]], expected: list[frozenset[str]]) -> float | None:
    if not expected:
        return None
    return sum(1 for rel in expected if rel in found_pairs) / len(expected)


def cross_domain_recall_lift(
    *,
    deterministic_pairs: list[frozenset[str]],
    inferred_edges: list[JudgedEdge],
    expected_relations: list[frozenset[str]],
) -> JudgeReport:
    """Cross-domain evidence-recall LIFT from the INFERRED edges vs deterministic-only.

    deterministic_pairs are the cross-domain relations the deterministic linker already
    found; the INFERRED edges (INFERRED_* buckets only) are added on top. The lift is
    combined_recall - deterministic_recall — the cross-domain reach phase 3B buys."""
    det = set(deterministic_pairs)
    inferred = [e for e in inferred_edges if e.is_inferred_edge]
    combined = det | {e.unordered for e in inferred}
    det_recall = _recall(det, expected_relations)
    combined_recall = _recall(combined, expected_relations)
    lift = (
        combined_recall - det_recall
        if det_recall is not None and combined_recall is not None
        else None
    )
    return JudgeReport(
        inferred_edges=len(inferred),
        inferred_edge_precision=inferred_edge_precision(inferred_edges),
        deterministic_recall=det_recall,
        combined_recall=combined_recall,
        recall_lift=lift,
    )


__all__ = [
    "INFERRED_BUCKETS",
    "INFERRED_PRECISION_GATE",
    "MIN_RECALL_LIFT",
    "JudgeReport",
    "JudgedEdge",
    "cross_domain_recall_lift",
    "inferred_edge_precision",
]
