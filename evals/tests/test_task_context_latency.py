"""get_task_context latency probe aggregation (harness.task_context_latency.summarize) — pure,
no database. The DB-calling half (`probe`) is exercised live by T2 (see harness.tiers.run_t2)."""

from harness.task_context_latency import LatencyResult, summarize


def test_summarize_empty_is_not_measured_never_faked() -> None:
    report = summarize([])
    assert report.n == 0
    assert report.p50_seconds is None
    assert report.p95_seconds is None
    assert report.errors == ()


def test_summarize_computes_p50_and_p95_over_successful_calls_only() -> None:
    results = [LatencyResult(task=f"t{i}", seconds=float(i)) for i in range(1, 11)]  # 1..10
    report = summarize(results)
    assert report.n == 10
    assert report.p50_seconds == 5.0
    assert report.p95_seconds == 10.0
    assert report.errors == ()


def test_summarize_excludes_errored_calls_from_the_latency_stats_but_lists_them() -> None:
    results = [
        LatencyResult(task="ok-1", seconds=1.0),
        LatencyResult(task="ok-2", seconds=2.0),
        LatencyResult(task="ok-3", seconds=3.0),
        LatencyResult(task="boom", seconds=None, error="RuntimeError: kaboom"),
    ]
    report = summarize(results)
    assert report.n == 4
    assert report.p50_seconds == 2.0  # only the three successful calls feed the percentile
    assert report.errors == ("boom",)


def test_summarize_all_errors_reports_no_latency_not_a_faked_zero() -> None:
    results = [LatencyResult(task="boom", seconds=None, error="TimeoutError: slow")]
    report = summarize(results)
    assert report.n == 1
    assert report.p50_seconds is None
    assert report.p95_seconds is None
    assert report.errors == ("boom",)


def test_summarize_single_result_uses_it_for_both_percentiles() -> None:
    report = summarize([LatencyResult(task="only", seconds=3.5)])
    assert report.p50_seconds == 3.5
    assert report.p95_seconds == 3.5
