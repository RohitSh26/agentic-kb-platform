"""Pure stdout/JSON parsing for the T1/T2/T3 tier checks — fed captured strings, no subprocess."""

import json
from pathlib import Path

from harness.tier_parsers import (
    parse_alias_output,
    parse_run_py_report,
    parse_task_context_ab_output,
)

ALIAS_STDOUT = """\
HIT   durable-cache-fix   query='the durable cache fix'   top1=services/x.py
MISS  some-miss-case      query='what changed'             top1=(no match)

25/25 top-1 hits = 100.0%
PASS (target >= 80%)
"""

AB_STDOUT = """\
## kb-search-dual-budget  (expected files: 4)
-- arm=tooled
   coverage=1.00 tool_cover=1.00 steps=2 reads=0 tokens=2267
-- arm=raw
  · [model error, arm ends early: BadRequestError: 400]
   coverage=0.00 tool_cover=0.00 steps=1 reads=0 tokens=210

=== AGGREGATE (mean over cases) ===
   tooled  coverage=0.400 tool_cover=0.400 steps=1.7 reads=2.7 tokens=2245
   raw     coverage=0.000 tool_cover=0.000 steps=2.0 reads=4.2 tokens=1893
"""


def test_parse_alias_output_extracts_the_summary_line() -> None:
    metrics = parse_alias_output(ALIAS_STDOUT)
    assert metrics == {
        "hits": "25",
        "cases": "25",
        "top1_accuracy_pct": "100.0",
        "verdict": "PASS",
    }


def test_parse_alias_output_missing_summary_returns_empty() -> None:
    assert parse_alias_output("nothing useful here") == {}


def test_parse_task_context_ab_output_extracts_the_aggregate_block_per_arm() -> None:
    metrics = parse_task_context_ab_output(AB_STDOUT)
    assert metrics["tooled_coverage"] == "0.400"
    assert metrics["tooled_tokens"] == "2245"
    assert metrics["raw_coverage"] == "0.000"
    assert metrics["raw_tokens"] == "1893"


def test_parse_task_context_ab_output_counts_flakes_without_hiding_them() -> None:
    metrics = parse_task_context_ab_output(AB_STDOUT)
    assert metrics["flakes"] == "1"


def test_parse_task_context_ab_output_omits_flakes_key_when_there_are_none() -> None:
    clean = AB_STDOUT.replace("  · [model error, arm ends early: BadRequestError: 400]\n", "")
    metrics = parse_task_context_ab_output(clean)
    assert "flakes" not in metrics


def test_parse_run_py_report_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_run_py_report(tmp_path / "does-not-exist.json") == {}


def test_parse_run_py_report_invalid_json_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "report.json"
    bad.write_text("{not json", encoding="utf-8")
    assert parse_run_py_report(bad) == {}


def test_parse_run_py_report_extracts_the_stable_subset(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "cases": [{"id": "a", "succeeded": True}, {"id": "b", "succeeded": False}],
                "golden": {"cases": 5, "mean_evidence_recall": 0.96, "total_acl_leaks": 0},
                "baseline": {"verdict": "flat"},
            }
        ),
        encoding="utf-8",
    )
    metrics = parse_run_py_report(report_path)
    assert metrics == {
        "cases": "1/2 succeeded",
        "golden_cases": "5",
        "golden_mean_evidence_recall": "0.960",
        "golden_acl_leaks": "0",
        "baseline_verdict": "flat",
    }


def test_parse_run_py_report_handles_a_null_evidence_recall_without_faking_a_number(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps({"cases": [], "golden": {"cases": 0, "mean_evidence_recall": None}}),
        encoding="utf-8",
    )
    metrics = parse_run_py_report(report_path)
    assert metrics["golden_mean_evidence_recall"] == "n/a"
