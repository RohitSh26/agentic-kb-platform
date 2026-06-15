"""Golden publish-gate wiring (Evals-F8/F5).

These pin the half that run.py owns: that the golden-set report drives the
exit-1 publish gate (sub-floor evidence-recall OR any ACL leak) and that the
report.json `golden` block carries the contract fields. The per-case metric
math is already pinned in test_golden.py — here we test the gate + serialization,
not the metrics, so no DB is needed.
"""

import json

from harness.baseline import compare
from harness.golden import GoldenCase, GoldenReport, GoldenResult, aggregate
from harness.metrics import compute_metrics
from harness.report import build_report
from run import _golden_gate_failures


def _case(case_id: str, expected: list[str], **overrides: object) -> GoldenCase:
    base: dict[str, object] = {
        "case_id": case_id,
        "query": "where is x",
        "intent": "how_does_x_work",
        "expected_evidence_ids": expected,
    }
    base.update(overrides)
    return GoldenCase.model_validate(base)


def test_gate_passes_when_all_cases_meet_floor_and_no_leak() -> None:
    case = _case("g/ok", ["ev_a", "ev_b"], min_evidence_recall=1.0)
    report = aggregate([GoldenResult(case=case, returned_evidence_ids=frozenset({"ev_a", "ev_b"}))])
    assert report.mean_evidence_recall == 1.0
    assert _golden_gate_failures(report) == []


def test_gate_fails_on_below_floor_case() -> None:
    case = _case("g/underlinked", ["ev_a", "ev_b"], min_evidence_recall=1.0)
    # only one of two expected ids surfaced ⇒ recall 0.5 < floor ⇒ a publish failure.
    report = aggregate([GoldenResult(case=case, returned_evidence_ids=frozenset({"ev_a"}))])
    failures = _golden_gate_failures(report)
    assert any("g/underlinked" in f for f in failures)
    assert any("below floor" in f for f in failures)


def test_gate_fails_on_acl_leak_even_at_full_recall() -> None:
    case = _case("g/leaky", ["ev_a"], must_not_leak_ids=["ev_secret"])
    report = aggregate(
        [GoldenResult(case=case, returned_evidence_ids=frozenset({"ev_a", "ev_secret"}))]
    )
    assert report.total_acl_leaks == 1
    failures = _golden_gate_failures(report)
    assert any("ACL leak" in f for f in failures)


def test_empty_golden_report_does_not_fail_the_gate() -> None:
    assert _golden_gate_failures(aggregate([])) == []


def test_report_carries_golden_block() -> None:
    case = _case("g/ok", ["ev_a", "ev_b"])
    golden = aggregate([GoldenResult(case=case, returned_evidence_ids=frozenset({"ev_a", "ev_b"}))])
    metrics = compute_metrics([])
    report = build_report([], metrics, compare(None, metrics), git_sha=None, golden=golden)

    serialized = json.loads(json.dumps(report))  # strict-JSON round-trip
    block = serialized["golden"]
    assert block["cases"] == 1
    assert block["mean_evidence_recall"] == 1.0
    assert block["min_evidence_recall"] == 1.0
    assert block["total_acl_leaks"] == 0
    assert block["cases_below_floor"] == []
    assert block["intent_ordering_failures"] == []


def test_report_golden_block_is_null_when_no_golden_set() -> None:
    metrics = compute_metrics([])
    report = build_report([], metrics, compare(None, metrics), git_sha=None, golden=None)
    block = report["golden"]
    assert isinstance(block, dict)
    assert block["cases"] == 0
    # never a faked number when nothing was measured (the not_measured discipline).
    assert block["mean_evidence_recall"] is None
    assert block["total_acl_leaks"] == 0


def test_below_floor_and_leak_both_reported() -> None:
    bad = _case("g/bad", ["ev_a", "ev_b"], must_not_leak_ids=["ev_x"], min_evidence_recall=1.0)
    report: GoldenReport = aggregate(
        [GoldenResult(case=bad, returned_evidence_ids=frozenset({"ev_a", "ev_x"}))]
    )
    failures = _golden_gate_failures(report)
    assert len(failures) == 2  # one floor failure, one ACL-leak failure
