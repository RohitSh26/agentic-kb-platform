"""Candidate-generator quality metrics (PR-28, docs/contracts/relationship-candidates.md).

Pins the phase-3A go/no-go numbers — recall against the cross-domain golden expectations,
sampled precision, volume per artifact, and cost-if-judged — over a fixed candidate set.
"""

from harness.candidate_fixture import EXPECTED_RELATIONS, GENERATED_CANDIDATES
from harness.candidates import (
    JUDGE_TOKENS_PER_CANDIDATE,
    CandidatePair,
    aggregate_candidates,
    candidate_precision,
    candidate_recall,
    cost_if_judged,
    volume_per_artifact,
)


def _pair(a: str, b: str, relevant: bool | None = None) -> CandidatePair:
    return CandidatePair(a, b, is_relevant=relevant)


def _rel(a: str, b: str) -> frozenset[str]:
    return frozenset((a, b))


def test_recall_is_fraction_of_expected_relations_surfaced() -> None:
    expected = [_rel("c", "card"), _rel("c", "code")]
    candidates = [_pair("c", "card"), _pair("x", "y")]
    assert candidate_recall(candidates, expected) == 0.5


def test_recall_direction_insensitive() -> None:
    # the generator may emit a pair in either order; recall is over unordered pairs.
    expected = [_rel("commit", "card")]
    candidates = [_pair("card", "commit")]
    assert candidate_recall(candidates, expected) == 1.0


def test_recall_none_when_no_expected_relations() -> None:
    assert candidate_recall([_pair("a", "b")], []) is None


def test_precision_is_sampled_over_judged_candidates() -> None:
    candidates = [
        _pair("a", "b", relevant=True),
        _pair("c", "d", relevant=False),
        _pair("e", "f", relevant=None),  # not sampled — excluded from precision
    ]
    assert candidate_precision(candidates) == 0.5


def test_precision_none_when_nothing_sampled() -> None:
    assert candidate_precision([_pair("a", "b")]) is None


def test_volume_per_artifact_is_candidates_over_from_artifacts() -> None:
    candidates = [_pair("a", "x"), _pair("a", "y"), _pair("b", "z")]
    assert volume_per_artifact(candidates) == 1.5


def test_volume_none_when_no_candidates() -> None:
    assert volume_per_artifact([]) is None


def test_cost_if_judged_is_count_times_per_candidate() -> None:
    candidates = [_pair("a", "b"), _pair("c", "d")]
    assert cost_if_judged(candidates) == 2 * JUDGE_TOKENS_PER_CANDIDATE


def test_aggregate_reports_all_metrics_and_missing_relations() -> None:
    expected = [_rel("c", "card"), _rel("c", "code")]
    candidates = [_pair("c", "card", relevant=True), _pair("c", "extra", relevant=False)]
    report = aggregate_candidates(candidates, expected)
    assert report.candidate_count == 2
    assert report.from_artifacts == 1
    assert report.expected_relations == 2
    assert report.recall == 0.5
    assert report.precision == 0.5
    assert report.volume_per_artifact == 2.0
    assert report.cost_if_judged_tokens == 2 * JUDGE_TOKENS_PER_CANDIDATE
    assert report.missing_relations == (_rel("c", "code"),)


def test_golden_cross_domain_fixture_has_full_recall() -> None:
    # the cheap generator surfaces every cross-domain golden relation (recall 1.0) —
    # the phase-3A go signal that judging (phase 3B) is worth its tokens.
    report = aggregate_candidates(GENERATED_CANDIDATES, EXPECTED_RELATIONS)
    assert report.recall == 1.0
    assert report.missing_relations == ()
    assert report.precision is not None and 0.0 <= report.precision <= 1.0
    assert report.volume_per_artifact is not None
    assert report.cost_if_judged_tokens == len(GENERATED_CANDIDATES) * JUDGE_TOKENS_PER_CANDIDATE
