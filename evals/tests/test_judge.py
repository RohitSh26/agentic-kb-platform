"""Relationship-judge quality metrics (PR-29, docs/contracts/relationship-judgment.md).

Pins the phase-3B go numbers — inferred-edge precision + cross-domain evidence-recall LIFT
over the deterministic-only baseline — over the fixed judged-edge fixture.
"""

from harness.judge import (
    INFERRED_PRECISION_GATE,
    MIN_RECALL_LIFT,
    JudgedEdge,
    cross_domain_recall_lift,
    inferred_edge_precision,
)
from harness.judge_fixture import DETERMINISTIC_PAIRS, EXPECTED_RELATIONS, JUDGED_EDGES


def _rel(a: str, b: str) -> frozenset[str]:
    return frozenset((a, b))


def test_precision_is_sampled_over_inferred_edges_only() -> None:
    # AMBIGUOUS/REJECTED never become edges, so they are excluded from precision.
    edges = [
        JudgedEdge("a", "b", "INFERRED_HIGH", is_relevant=True),
        JudgedEdge("c", "d", "INFERRED_LOW", is_relevant=False),
        JudgedEdge("e", "f", "REJECTED", is_relevant=False),  # not an edge -> excluded
    ]
    assert inferred_edge_precision(edges) == 0.5


def test_precision_none_when_no_inferred_edges_sampled() -> None:
    assert inferred_edge_precision([JudgedEdge("a", "b", "REJECTED")]) is None


def test_recall_lift_is_combined_minus_deterministic() -> None:
    expected = [_rel("commit", "card"), _rel("doc", "code")]
    deterministic = [_rel("commit", "card")]  # det recall 0.5
    inferred = [JudgedEdge("doc", "code", "INFERRED_HIGH", is_relevant=True)]  # adds the 2nd
    report = cross_domain_recall_lift(
        deterministic_pairs=deterministic,
        inferred_edges=inferred,
        expected_relations=expected,
    )
    assert report.deterministic_recall == 0.5
    assert report.combined_recall == 1.0
    assert report.recall_lift == 0.5


def test_rejected_and_ambiguous_do_not_lift_recall() -> None:
    expected = [_rel("doc", "code")]
    inferred = [
        JudgedEdge("doc", "code", "REJECTED"),
        JudgedEdge("doc", "code", "AMBIGUOUS"),
    ]
    report = cross_domain_recall_lift(
        deterministic_pairs=[],
        inferred_edges=inferred,
        expected_relations=expected,
    )
    # neither bucket is an edge, so the prose-only relation stays unreached.
    assert report.combined_recall == 0.0
    assert report.recall_lift == 0.0


def test_golden_judge_fixture_lifts_cross_domain_recall_without_low_precision() -> None:
    # the phase-3B go signal: the prose-only doc->code relation the deterministic linker
    # cannot reach is surfaced by an INFERRED edge, lifting recall, while precision stays
    # at/above the gate.
    report = cross_domain_recall_lift(
        deterministic_pairs=DETERMINISTIC_PAIRS,
        inferred_edges=JUDGED_EDGES,
        expected_relations=EXPECTED_RELATIONS,
    )
    assert report.deterministic_recall is not None
    assert report.combined_recall is not None
    assert report.recall_lift is not None and report.recall_lift > MIN_RECALL_LIFT
    assert report.combined_recall > report.deterministic_recall
    assert (
        report.inferred_edge_precision is not None
        and report.inferred_edge_precision >= INFERRED_PRECISION_GATE
    )
