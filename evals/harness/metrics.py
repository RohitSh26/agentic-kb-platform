"""Pure metric computation over RunRecords.

Metric names are pinned to docs/architecture §13 and docs/contracts/evals-report.md.
Build-plane metrics and human_plan_edit_rate cannot be measured by the runtime
harness and are emitted as not_measured with a null value — never faked.
"""

from dataclasses import dataclass
from typing import Literal

from harness.records import RunRecord

MetricStatus = Literal["measured", "measured_scripted", "not_measured"]

METRIC_NAMES: tuple[str, ...] = (
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

NOT_MEASURED: tuple[str, ...] = (
    "llm_calls_per_build",
    "embedding_calls_per_build",
    "human_plan_edit_rate",
    "active_kb_age",
)

LOWER_IS_BETTER = frozenset(
    {
        "context_tokens_per_successful_task",
        "duplicate_context_tokens",
        "retrieval_calls_per_agent",
        "unsupported_claim_rate",
        "missing_context_rate",
    }
)
HIGHER_IS_BETTER = frozenset({"evidence_reuse_rate", "semantic_cache_hit_rate"})

# the stable subset the eval-runner subagent reads (.claude/agents/eval-runner.md)
EVAL_RUNNER_METRICS: tuple[str, ...] = (
    "context_tokens_per_successful_task",
    "duplicate_context_tokens",
    "evidence_reuse_rate",
    "retrieval_calls_per_agent",
    "semantic_cache_hit_rate",
    "unsupported_claim_rate",
    "missing_context_rate",
)


@dataclass(frozen=True)
class MetricValue:
    value: float | None
    status: MetricStatus

    def as_dict(self) -> dict[str, float | str | None]:
        return {"value": self.value, "status": self.status}


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def compute_metrics(records: list[RunRecord]) -> dict[str, MetricValue]:
    events = [event for record in records for event in record.events]
    successful = [record for record in records if record.succeeded]

    reuse_eligible = [e for e in events if e.status in ("reused", "approved")]
    follow_ups = [e for e in events if e.tool_name == "context.request_more"]
    agents = {event.agent_name for event in events}

    measured: dict[str, float | None] = {
        "context_tokens_per_successful_task": _ratio(
            sum(record.tokens_charged for record in successful), len(successful)
        ),
        "duplicate_context_tokens": float(_duplicate_tokens(records)),
        "evidence_reuse_rate": _ratio(
            sum(1 for e in reuse_eligible if e.status == "reused"), len(reuse_eligible)
        ),
        "retrieval_calls_per_agent": _ratio(len(events), len(agents)),
        "semantic_cache_hit_rate": _ratio(
            sum(1 for e in follow_ups if e.semantic_reuse), len(follow_ups)
        ),
        "unsupported_claim_rate": _ratio(
            sum(record.unsupported_claims for record in records),
            sum(record.total_claims for record in records),
        ),
        "missing_context_rate": _ratio(
            sum(len(record.missing_items) for record in records),
            sum(record.expected_items for record in records),
        ),
    }

    metrics: dict[str, MetricValue] = {}
    for name in METRIC_NAMES:
        if name in NOT_MEASURED:
            metrics[name] = MetricValue(value=None, status="not_measured")
        elif name == "unsupported_claim_rate":
            metrics[name] = MetricValue(value=measured[name], status="measured_scripted")
        else:
            metrics[name] = MetricValue(value=measured[name], status="measured")
    return metrics


def _duplicate_tokens(records: list[RunRecord]) -> int:
    """Tokens charged for evidence already delivered earlier in the same run.

    Cards (L0/L1) and expansions (L2) are tracked separately: the first
    open_evidence on a carded id is new content, not a duplicate. The broker's
    dedupe should keep this at 0 — the metric exists to catch regressions.
    Mixed deliveries (some ids new, some already delivered) count zero:
    tokens_returned is not splittable per id, so the metric under- rather
    than over-counts.
    """
    duplicate = 0
    for record in records:
        carded: set[str] = set()
        expanded: set[str] = set()
        for event in record.events:
            ids = set(event.evidence_ids)
            if event.status == "approved" and event.tokens_returned > 0 and ids:
                already = expanded if event.tool_name == "context.open_evidence" else carded
                if ids <= already:
                    duplicate += event.tokens_returned
            if event.tool_name == "context.open_evidence":
                if event.status == "approved":
                    expanded |= ids
            else:
                carded |= ids
    return duplicate


def per_agent_calls(records: list[RunRecord]) -> dict[str, int]:
    calls: dict[str, int] = {}
    for record in records:
        for event in record.events:
            calls[event.agent_name] = calls.get(event.agent_name, 0) + 1
    return dict(sorted(calls.items()))
