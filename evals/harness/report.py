"""Report assembly + the stdout table the eval-runner subagent reads."""

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from harness.baseline import BaselineComparison
from harness.golden import GoldenReport
from harness.metrics import METRIC_NAMES, MetricValue, per_agent_calls
from harness.records import RunRecord

REPORT_SCHEMA_VERSION = "1.0.0"


def _golden_block(golden: GoldenReport | None) -> dict[str, object]:
    """The publish-gate (evidence-recall) block, documented in evals-report.md.

    ``cases`` 0 (golden set absent) ⇒ null recall fields, never a faked number —
    mirrors the not_measured discipline of the metric block."""
    if golden is None:
        golden = GoldenReport(
            cases=0,
            mean_evidence_recall=None,
            min_evidence_recall=None,
            total_acl_leaks=0,
            cases_below_floor=(),
            edge_precision={},
            edge_recall={},
            intent_ordering_failures=(),
        )
    return {
        "cases": golden.cases,
        "mean_evidence_recall": golden.mean_evidence_recall,
        "min_evidence_recall": golden.min_evidence_recall,
        "total_acl_leaks": golden.total_acl_leaks,
        "cases_below_floor": list(golden.cases_below_floor),
        "intent_ordering_failures": list(golden.intent_ordering_failures),
    }


def build_report(
    records: list[RunRecord],
    metrics: dict[str, MetricValue],
    comparison: BaselineComparison,
    git_sha: str | None,
    golden: GoldenReport | None = None,
) -> dict[str, object]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha,
        "cases": [
            {
                "id": record.case_id,
                "task_type": record.task_type,
                "succeeded": record.succeeded,
                "missing": list(record.missing_items),
                "tokens_charged": record.tokens_charged,
            }
            for record in records
        ],
        "metrics": {name: metrics[name].as_dict() for name in METRIC_NAMES},
        "golden": _golden_block(golden),
        "per_agent_calls": per_agent_calls(records),
        "baseline": {
            "present": comparison.present,
            "deltas": {
                name: {
                    "old": delta.old,
                    "new": delta.new,
                    # infinite relative (old == 0) becomes null: report.json stays strict JSON
                    "relative": delta.relative if math.isfinite(delta.relative) else None,
                }
                for name, delta in comparison.deltas.items()
            },
            "verdict": comparison.verdict,
            "biggest_mover": comparison.biggest_mover,
        },
    }


def write_report(path: Path, report: dict[str, object]) -> None:
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def render_table(
    records: list[RunRecord],
    metrics: dict[str, MetricValue],
    comparison: BaselineComparison,
) -> str:
    lines: list[str] = []
    succeeded = sum(1 for record in records if record.succeeded)
    lines.append(f"cases: {succeeded}/{len(records)} succeeded")
    for record in records:
        marker = "ok " if record.succeeded else "FAIL"
        missing = f" missing={','.join(record.missing_items)}" if record.missing_items else ""
        lines.append(f"  [{marker}] {record.case_id} tokens={record.tokens_charged}{missing}")

    lines.append(f"{'metric':<38} {'value':>10} {'delta':>10}")
    for name in METRIC_NAMES:
        metric = metrics[name]
        value = "n/a" if metric.value is None else f"{metric.value:.4g}"
        delta = comparison.deltas.get(name)
        delta_text = "-" if delta is None else f"{delta.relative:+.1%}"
        suffix = "" if metric.status == "measured" else f"  ({metric.status})"
        lines.append(f"{name:<38} {value:>10} {delta_text:>10}{suffix}")

    mover = f" — biggest mover: {comparison.biggest_mover}" if comparison.biggest_mover else ""
    lines.append(f"verdict: {comparison.verdict}{mover}")
    return "\n".join(lines)
