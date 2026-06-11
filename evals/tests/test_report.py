"""Report serialization: deltas must stay strict JSON."""

import json

from harness.baseline import compare
from harness.metrics import MetricValue, compute_metrics
from harness.report import build_report


def test_infinite_relative_delta_serializes_as_null() -> None:
    baseline = {"unsupported_claim_rate": MetricValue(value=0.0, status="measured_scripted")}
    metrics = compute_metrics([])
    current = dict(metrics)
    current["unsupported_claim_rate"] = MetricValue(value=0.25, status="measured_scripted")

    comparison = compare(baseline, current)
    report = build_report([], current, comparison, git_sha=None)

    serialized = json.dumps(report)
    parsed = json.loads(serialized)
    assert "Infinity" not in serialized
    assert parsed["baseline"]["deltas"]["unsupported_claim_rate"]["relative"] is None
    assert parsed["baseline"]["verdict"] == "regressed"
    assert parsed["baseline"]["biggest_mover"] == "unsupported_claim_rate"
