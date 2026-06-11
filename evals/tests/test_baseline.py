"""Baseline diffing: deltas, verdict thresholds, persistence roundtrip."""

import math
from pathlib import Path

import pytest

from harness.baseline import compare, load_baseline, write_baseline
from harness.metrics import MetricStatus, MetricValue


def mv(value: float | None, status: MetricStatus = "measured") -> MetricValue:
    return MetricValue(value=value, status=status)


def test_missing_baseline_yields_no_baseline_verdict() -> None:
    comparison = compare(None, {"duplicate_context_tokens": mv(0.0)})
    assert comparison.present is False
    assert comparison.verdict == "no_baseline"
    assert comparison.deltas == {}
    assert comparison.biggest_mover is None


def test_identical_runs_are_flat() -> None:
    metrics = {"duplicate_context_tokens": mv(10.0), "evidence_reuse_rate": mv(0.5)}
    comparison = compare(metrics, dict(metrics))
    assert comparison.verdict == "flat"
    assert all(delta.relative == 0.0 for delta in comparison.deltas.values())


def test_lower_is_better_increase_regresses() -> None:
    comparison = compare(
        {"duplicate_context_tokens": mv(100.0)}, {"duplicate_context_tokens": mv(110.0)}
    )
    assert comparison.verdict == "regressed"
    assert comparison.deltas["duplicate_context_tokens"].relative == 0.1


def test_lower_is_better_decrease_improves() -> None:
    comparison = compare(
        {"duplicate_context_tokens": mv(100.0)}, {"duplicate_context_tokens": mv(90.0)}
    )
    assert comparison.verdict == "improved"


def test_higher_is_better_decrease_regresses() -> None:
    comparison = compare({"evidence_reuse_rate": mv(0.5)}, {"evidence_reuse_rate": mv(0.4)})
    assert comparison.verdict == "regressed"


def test_higher_is_better_increase_improves() -> None:
    comparison = compare({"evidence_reuse_rate": mv(0.5)}, {"evidence_reuse_rate": mv(0.6)})
    assert comparison.verdict == "improved"


def test_movement_within_five_percent_is_flat() -> None:
    comparison = compare(
        {"context_tokens_per_successful_task": mv(100.0)},
        {"context_tokens_per_successful_task": mv(104.0)},
    )
    assert comparison.verdict == "flat"


def test_any_regression_outweighs_improvements() -> None:
    comparison = compare(
        {"duplicate_context_tokens": mv(100.0), "evidence_reuse_rate": mv(0.5)},
        {"duplicate_context_tokens": mv(50.0), "evidence_reuse_rate": mv(0.3)},
    )
    assert comparison.verdict == "regressed"


def test_null_metrics_are_excluded_from_deltas() -> None:
    comparison = compare(
        {"llm_calls_per_build": mv(None, "not_measured"), "missing_context_rate": mv(0.0)},
        {"llm_calls_per_build": mv(None, "not_measured"), "missing_context_rate": mv(0.0)},
    )
    assert "llm_calls_per_build" not in comparison.deltas
    assert "missing_context_rate" in comparison.deltas


def test_metric_absent_from_baseline_is_skipped() -> None:
    comparison = compare({}, {"missing_context_rate": mv(0.0)})
    assert comparison.deltas == {}
    assert comparison.verdict == "flat"


def test_zero_baseline_to_nonzero_is_infinite_regression() -> None:
    comparison = compare({"unsupported_claim_rate": mv(0.0)}, {"unsupported_claim_rate": mv(0.25)})
    assert math.isinf(comparison.deltas["unsupported_claim_rate"].relative)
    assert comparison.verdict == "regressed"
    assert comparison.biggest_mover == "unsupported_claim_rate"


def test_biggest_mover_is_largest_absolute_relative_delta() -> None:
    comparison = compare(
        {"duplicate_context_tokens": mv(100.0), "evidence_reuse_rate": mv(0.5)},
        {"duplicate_context_tokens": mv(101.0), "evidence_reuse_rate": mv(1.0)},
    )
    assert comparison.biggest_mover == "evidence_reuse_rate"


def test_write_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    metrics = {
        "missing_context_rate": mv(0.0),
        "unsupported_claim_rate": mv(0.1, "measured_scripted"),
        "llm_calls_per_build": mv(None, "not_measured"),
    }
    write_baseline(path, metrics, git_sha="abc1234")
    assert load_baseline(path) == metrics


def test_load_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_baseline(tmp_path / "absent.json") is None


def test_load_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(
        '{"schema_version": "9.9.9", "metrics": {}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema_version"):
        load_baseline(path)
