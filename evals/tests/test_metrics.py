"""Metric computation on synthetic RunRecords (no database)."""

from harness.metrics import NOT_MEASURED, compute_metrics, per_agent_calls
from harness.records import LedgerEvent, RunRecord


def event(
    *,
    tool: str = "context.request_more",
    status: str = "approved",
    agent: str = "impl-agent",
    cache_hit: bool = False,
    semantic: bool = False,
    tokens: int = 0,
    reused: tuple[str, ...] = (),
    new: tuple[str, ...] = (),
) -> LedgerEvent:
    return LedgerEvent(
        tool_name=tool,
        status=status,
        agent_name=agent,
        cache_hit=cache_hit,
        semantic_reuse=semantic,
        tokens_returned=tokens,
        reused_evidence_ids=reused,
        new_evidence_ids=new,
    )


def record(
    *,
    case_id: str = "case-a",
    succeeded: bool = True,
    expected: int = 1,
    missing: tuple[str, ...] = (),
    total_claims: int = 0,
    unsupported: int = 0,
    events: tuple[LedgerEvent, ...] = (),
) -> RunRecord:
    return RunRecord(
        case_id=case_id,
        task_type="retrieval_recall",
        succeeded=succeeded,
        expected_items=expected,
        missing_items=missing,
        total_claims=total_claims,
        unsupported_claims=unsupported,
        events=events,
    )


def test_context_tokens_averages_only_successful_tasks() -> None:
    records = [
        record(case_id="ok-1", events=(event(tokens=60), event(tokens=40))),
        record(case_id="failed", succeeded=False, events=(event(tokens=500),)),
    ]
    metrics = compute_metrics(records)
    assert metrics["context_tokens_per_successful_task"].value == 100.0
    assert metrics["context_tokens_per_successful_task"].status == "measured"


def test_context_tokens_null_when_nothing_succeeded() -> None:
    metrics = compute_metrics([record(succeeded=False, events=(event(tokens=50),))])
    assert metrics["context_tokens_per_successful_task"].value is None


def test_evidence_reuse_rate_excludes_denials_from_denominator() -> None:
    records = [
        record(
            events=(
                event(status="reused", cache_hit=True),
                event(status="approved"),
                event(status="denied"),
                event(status="needs_human_approval"),
            )
        )
    ]
    assert compute_metrics(records)["evidence_reuse_rate"].value == 0.5


def test_evidence_reuse_rate_null_without_eligible_events() -> None:
    records = [record(events=(event(status="denied"),))]
    assert compute_metrics(records)["evidence_reuse_rate"].value is None


def test_semantic_cache_hit_rate_counts_only_request_more_calls() -> None:
    records = [
        record(
            events=(
                event(tool="context.create_pack", agent="orchestrator"),
                event(semantic=True, status="reused"),
                event(semantic=False),
            )
        )
    ]
    assert compute_metrics(records)["semantic_cache_hit_rate"].value == 0.5


def test_retrieval_calls_per_agent_counts_orchestrator_too() -> None:
    records = [
        record(
            events=(
                event(agent="orchestrator", tool="context.create_pack"),
                event(agent="impl-agent"),
                event(agent="impl-agent"),
                event(agent="impl-agent"),
                event(agent="test-agent"),
                event(agent="test-agent"),
            )
        )
    ]
    assert compute_metrics(records)["retrieval_calls_per_agent"].value == 2.0


def test_unsupported_claim_rate_is_labeled_measured_scripted() -> None:
    records = [
        record(case_id="a", total_claims=3, unsupported=1),
        record(case_id="b", total_claims=1, unsupported=0),
    ]
    metric = compute_metrics(records)["unsupported_claim_rate"]
    assert metric.value == 0.25
    assert metric.status == "measured_scripted"


def test_missing_context_rate_over_all_expected_items() -> None:
    records = [
        record(case_id="a", expected=4, missing=("file:x.py",), succeeded=True),
        record(case_id="b", expected=2),
    ]
    metrics = compute_metrics(records)
    assert metrics["missing_context_rate"].value == 1 / 6


def test_not_measured_metrics_are_null_never_faked() -> None:
    metrics = compute_metrics([record(events=(event(tokens=10),))])
    for name in NOT_MEASURED:
        assert metrics[name].value is None
        assert metrics[name].status == "not_measured"


def test_empty_run_yields_null_ratios_and_zero_duplicates() -> None:
    metrics = compute_metrics([])
    assert metrics["context_tokens_per_successful_task"].value is None
    assert metrics["evidence_reuse_rate"].value is None
    assert metrics["retrieval_calls_per_agent"].value is None
    assert metrics["missing_context_rate"].value is None
    assert metrics["duplicate_context_tokens"].value == 0.0


def test_first_expansion_of_a_carded_id_is_not_a_duplicate() -> None:
    records = [
        record(
            events=(
                event(tool="context.create_pack", agent="orchestrator", tokens=50, new=("a",)),
                event(tool="context.open_evidence", tokens=30, new=("a",)),
            )
        )
    ]
    assert compute_metrics(records)["duplicate_context_tokens"].value == 0.0


def test_repeated_expansion_of_same_id_counts_as_duplicate() -> None:
    records = [
        record(
            events=(
                event(tool="context.create_pack", agent="orchestrator", tokens=50, new=("a",)),
                event(tool="context.open_evidence", tokens=30, new=("a",)),
                event(tool="context.open_evidence", tokens=20, new=("a",)),
            )
        )
    ]
    assert compute_metrics(records)["duplicate_context_tokens"].value == 20.0


def test_recharged_card_delivery_counts_as_duplicate() -> None:
    records = [
        record(
            events=(
                event(tool="context.create_pack", agent="orchestrator", tokens=50, new=("a",)),
                event(tokens=30, new=("a",)),
            )
        )
    ]
    assert compute_metrics(records)["duplicate_context_tokens"].value == 30.0


def test_free_reuse_is_not_a_duplicate() -> None:
    records = [
        record(
            events=(
                event(tool="context.create_pack", agent="orchestrator", tokens=50, new=("a",)),
                event(status="reused", cache_hit=True, tokens=0, reused=("a",)),
            )
        )
    ]
    assert compute_metrics(records)["duplicate_context_tokens"].value == 0.0


def test_duplicates_are_tracked_per_case_not_across_cases() -> None:
    delivery = (event(tool="context.create_pack", agent="orchestrator", tokens=50, new=("a",)),)
    records = [record(case_id="a", events=delivery), record(case_id="b", events=delivery)]
    assert compute_metrics(records)["duplicate_context_tokens"].value == 0.0


def test_per_agent_calls_sorted_counts() -> None:
    records = [
        record(
            events=(
                event(agent="orchestrator", tool="context.create_pack"),
                event(agent="impl-agent"),
                event(agent="impl-agent"),
            )
        )
    ]
    assert per_agent_calls(records) == {"impl-agent": 2, "orchestrator": 1}
