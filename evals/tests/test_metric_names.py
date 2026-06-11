"""Pins the §13 metric names and the eval-runner's stable subset.

If one of these tests fails you are renaming a contract surface: update
docs/contracts/evals-report.md and .claude/agents/eval-runner.md in the same change.
"""

from harness.metrics import (
    EVAL_RUNNER_METRICS,
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    METRIC_NAMES,
    NOT_MEASURED,
)


def test_metric_names_match_architecture_section_13() -> None:
    assert METRIC_NAMES == (
        "context_tokens_per_successful_task",
        "duplicate_context_tokens",
        "evidence_reuse_rate",
        "retrieval_calls_per_agent",
        "semantic_cache_hit_rate",
        "llm_calls_per_build",
        "embedding_calls_per_build",
        "unsupported_claim_rate",
        "human_plan_edit_rate",
        "missing_context_rate",
        "active_kb_age",
    )


def test_eval_runner_subset_is_stable() -> None:
    assert EVAL_RUNNER_METRICS == (
        "context_tokens_per_successful_task",
        "duplicate_context_tokens",
        "evidence_reuse_rate",
        "retrieval_calls_per_agent",
        "semantic_cache_hit_rate",
        "unsupported_claim_rate",
        "missing_context_rate",
    )
    assert set(EVAL_RUNNER_METRICS) <= set(METRIC_NAMES)


def test_not_measured_metrics_are_known_names() -> None:
    assert set(NOT_MEASURED) <= set(METRIC_NAMES)


def test_directionality_covers_exactly_the_comparable_metrics() -> None:
    assert LOWER_IS_BETTER.isdisjoint(HIGHER_IS_BETTER)
    assert set(METRIC_NAMES) - set(NOT_MEASURED) == LOWER_IS_BETTER | HIGHER_IS_BETTER
    assert set(EVAL_RUNNER_METRICS) == LOWER_IS_BETTER | HIGHER_IS_BETTER
