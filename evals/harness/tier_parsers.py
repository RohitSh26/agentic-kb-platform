"""Pure stdout/JSON parsing for the consolidated runner's tier checks.

Kept separate from harness.tiers (which does the actual subprocess/DB I/O) so the parsing
logic — the part most likely to need a regex tweak when a script's print format changes — is
unit-testable against captured strings, no subprocess or database required. These functions
extract a compact METRICS subset for the report table; they never replace the verbatim
stdout/stderr a failing check's `detail` carries (see harness.tier_result).
"""

import json
import re
from pathlib import Path

_ALIAS_SUMMARY_RE = re.compile(r"(\d+)/(\d+) top-1 hits = ([\d.]+)%")
_ALIAS_VERDICT_RE = re.compile(r"^(PASS|FAIL) \(target >= [\d.]+%\)", re.MULTILINE)

_AB_AGGREGATE_RE = re.compile(
    r"^\s+(?P<arm>tooled|raw)\s+coverage=(?P<coverage>[\d.]+)\s+tool_cover=(?P<tool_cover>[\d.]+)\s+"
    r"steps=(?P<steps>[\d.]+)\s+reads=(?P<reads>[\d.]+)\s+tokens=(?P<tokens>[\d.]+)",
    re.MULTILINE,
)


def parse_alias_output(stdout: str) -> dict[str, str]:
    """scripts/eval_alias_resolution.py's stdout -> {hits, cases, top1_accuracy_pct, verdict}."""
    metrics: dict[str, str] = {}
    summary = _ALIAS_SUMMARY_RE.search(stdout)
    if summary is not None:
        metrics["hits"] = summary.group(1)
        metrics["cases"] = summary.group(2)
        metrics["top1_accuracy_pct"] = summary.group(3)
    verdict = _ALIAS_VERDICT_RE.search(stdout)
    if verdict is not None:
        metrics["verdict"] = verdict.group(1)
    return metrics


def parse_task_context_ab_output(stdout: str) -> dict[str, str]:
    """scripts/eval_task_context.py's stdout -> per-arm aggregate metrics + flake count.

    Flakes (a model hallucinating a tool name -> provider 400, ending an arm-run early) are
    counted, never hidden: docs/reports/task-context-ab-2026-07-03.md ("flakes split evenly
    ... so the aggregate comparison is not biased") is the house precedent for reporting this
    honestly instead of dropping the failed runs from the average.
    """
    metrics: dict[str, str] = {}
    for match in _AB_AGGREGATE_RE.finditer(stdout):
        arm = match.group("arm")
        metrics[f"{arm}_coverage"] = match.group("coverage")
        metrics[f"{arm}_tool_cover"] = match.group("tool_cover")
        metrics[f"{arm}_steps"] = match.group("steps")
        metrics[f"{arm}_tokens"] = match.group("tokens")
    flakes = stdout.count("[model error")
    if flakes:
        metrics["flakes"] = str(flakes)
    return metrics


def parse_run_py_report(report_path: Path) -> dict[str, str]:
    """evals/report.json (written by run.py) -> a compact metrics subset for the tier row."""
    if not report_path.is_file():
        return {}
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    cases = data.get("cases", [])
    succeeded = sum(1 for case in cases if case.get("succeeded"))
    golden = data.get("golden", {})
    baseline = data.get("baseline", {})
    return {
        "cases": f"{succeeded}/{len(cases)} succeeded",
        "golden_cases": str(golden.get("cases", 0)),
        "golden_mean_evidence_recall": _fmt(golden.get("mean_evidence_recall")),
        "golden_acl_leaks": str(golden.get("total_acl_leaks", 0)),
        "baseline_verdict": str(baseline.get("verdict", "n/a")),
    }


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"
