"""Golden-query metrics + case shape (docs/contracts/golden-query-evals.md).

evidence_recall is the first-class anti-underlinking metric; these tests pin it,
acl_leak_count, per-edge-type precision/recall, the aggregate publish-gate inputs,
and the seed golden set loading.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.golden import (
    DEFAULT_MIN_EVIDENCE_RECALL,
    GoldenCase,
    GoldenResult,
    acl_leak_count,
    aggregate,
    edge_scores,
    evidence_recall,
    intent_ordering_ok,
    load_golden_cases,
    missing_expected,
)

EVALS_DIR = Path(__file__).resolve().parent.parent
GOLDEN_DIR = EVALS_DIR / "retrieval_cases" / "golden"


def _case(**overrides: object) -> GoldenCase:
    base: dict[str, object] = {
        "case_id": "code-structure/sample-001",
        "query": "Where is helper defined?",
        "intent": "how_does_x_work",
        "expected_evidence_ids": ["ev_a", "ev_b"],
    }
    base.update(overrides)
    return GoldenCase.model_validate(base)


def _result(case: GoldenCase, **overrides: object) -> GoldenResult:
    kwargs: dict[str, object] = {"returned_evidence_ids": frozenset()}
    kwargs.update(overrides)
    return GoldenResult(case=case, **kwargs)  # type: ignore[arg-type]


def test_evidence_recall_is_intersection_over_expected() -> None:
    case = _case(expected_evidence_ids=["ev_a", "ev_b", "ev_c", "ev_d"])
    result = _result(case, returned_evidence_ids=frozenset({"ev_a", "ev_b", "ev_x"}))
    assert evidence_recall(result) == 0.5


def test_full_recall_when_all_expected_returned() -> None:
    case = _case(expected_evidence_ids=["ev_a", "ev_b"])
    result = _result(case, returned_evidence_ids=frozenset({"ev_a", "ev_b", "ev_extra"}))
    assert evidence_recall(result) == 1.0


def test_underlinking_drops_recall_and_lists_missing() -> None:
    # the silent failure: a real citation returned, but the key symbol is missing.
    case = _case(expected_evidence_ids=["ev_key_symbol", "ev_b"])
    result = _result(case, returned_evidence_ids=frozenset({"ev_b", "ev_real_but_wrong"}))
    assert evidence_recall(result) == 0.5
    assert missing_expected(result) == ("ev_key_symbol",)


def test_acl_leak_count_counts_forbidden_ids_that_appeared() -> None:
    case = _case(expected_evidence_ids=["ev_a"], must_not_leak_ids=["ev_secret", "ev_other"])
    result = _result(case, returned_evidence_ids=frozenset({"ev_a", "ev_secret"}))
    assert acl_leak_count(result) == 1


def test_no_leak_is_zero() -> None:
    case = _case(must_not_leak_ids=["ev_secret"])
    result = _result(case, returned_evidence_ids=frozenset({"ev_a"}))
    assert acl_leak_count(result) == 0


def test_expected_and_leak_ids_must_be_disjoint() -> None:
    with pytest.raises(ValidationError, match="both expected and must-not-leak"):
        _case(expected_evidence_ids=["ev_a"], must_not_leak_ids=["ev_a"])


def test_default_min_recall_applied() -> None:
    assert _case().min_evidence_recall == DEFAULT_MIN_EVIDENCE_RECALL


def test_edge_scores_precision_and_recall_per_type() -> None:
    case = _case(expected_edge_types=["calls"])
    result = _result(
        case,
        returned_evidence_ids=frozenset({"ev_a", "ev_b"}),
        surfaced_edges={"calls": frozenset({("s1", "s2"), ("s3", "s4")})},
        expected_edges={"calls": frozenset({("s1", "s2")})},
    )
    (score,) = edge_scores(result)
    assert score.edge_type == "calls"
    assert score.precision == 0.5  # 1 correct of 2 surfaced
    assert score.recall == 1.0  # the 1 expected was found


def test_edge_scores_none_when_nothing_to_score() -> None:
    case = _case()
    result = _result(case, returned_evidence_ids=frozenset())
    assert edge_scores(result) == ()


def test_aggregate_flags_cases_below_floor() -> None:
    good = _result(_case(case_id="g/ok"), returned_evidence_ids=frozenset({"ev_a", "ev_b"}))
    bad = _result(_case(case_id="g/bad"), returned_evidence_ids=frozenset({"ev_a"}))
    report = aggregate([good, bad])
    assert report.cases == 2
    assert report.mean_evidence_recall == 0.75
    assert report.min_evidence_recall == 0.5
    assert report.cases_below_floor == ("g/bad",)


def test_aggregate_sums_acl_leaks() -> None:
    leaky = _result(
        _case(must_not_leak_ids=["ev_secret"]),
        returned_evidence_ids=frozenset({"ev_a", "ev_b", "ev_secret"}),
    )
    report = aggregate([leaky])
    assert report.total_acl_leaks == 1


def test_aggregate_empty_is_null_not_zero() -> None:
    report = aggregate([])
    assert report.cases == 0
    assert report.mean_evidence_recall is None
    assert report.cases_below_floor == ()


def test_intent_ordering_none_when_case_asserts_no_order() -> None:
    # recall-only cases (no ordered_kinds) are unaffected by PR-33 ordering.
    result = _result(_case(), returned_evidence_ids=frozenset({"ev_a"}))
    assert intent_ordering_ok(result) is None


def test_how_intent_requires_code_primary() -> None:
    case = _case(intent="how_does_x_work")
    good = _result(case, ordered_kinds=("code", "doc"))
    bad = _result(case, ordered_kinds=("doc", "code"))
    assert intent_ordering_ok(good) is True
    assert intent_ordering_ok(bad) is False


def test_how_intent_fails_when_stale_doc_primary() -> None:
    case = _case(intent="how_does_x_work")
    # primary kind is code but a stale doc was surfaced as primary evidence
    result = _result(case, ordered_kinds=("code", "doc"), stale_primary=True)
    assert intent_ordering_ok(result) is False


def test_why_intent_requires_history_kinds_present() -> None:
    case = _case(intent="why_was_x_changed")
    with_card = _result(case, ordered_kinds=("card", "code"))
    code_only = _result(case, ordered_kinds=("code",))
    assert intent_ordering_ok(with_card) is True
    assert intent_ordering_ok(code_only) is False


def test_aggregate_collects_ordering_failures() -> None:
    good = _result(
        _case(case_id="g/ok", intent="how_does_x_work"),
        returned_evidence_ids=frozenset({"ev_a", "ev_b"}),
        ordered_kinds=("code", "doc"),
    )
    bad = _result(
        _case(case_id="g/bad-order", intent="how_does_x_work"),
        returned_evidence_ids=frozenset({"ev_a", "ev_b"}),
        ordered_kinds=("doc", "code"),
    )
    report = aggregate([good, bad])
    assert report.intent_ordering_failures == ("g/bad-order",)


def test_seed_golden_set_loads_with_unique_ids() -> None:
    cases = load_golden_cases(GOLDEN_DIR)
    assert len(cases) >= 1
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids))
    # the seed covers code-structure queries (where/what-calls/imports)
    assert all(c.expected_evidence_ids for c in cases)
