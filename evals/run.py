"""Eval harness entrypoint (docs/contracts/evals-report.md).

Runs retrieval + agent-task cases through the real Context Broker against a
migrated TEST_DATABASE_URL registry, computes the §13 metrics, diffs against
the committed baseline, writes report.json, and prints the table the
eval-runner subagent reads.
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from harness.baseline import compare, load_baseline, write_baseline
from harness.candidate_fixture import EXPECTED_RELATIONS, GENERATED_CANDIDATES
from harness.candidates import aggregate_candidates
from harness.cases import BENCHMARK_TASK_TYPES, EvalCase, load_cases
from harness.dashboard import generate_dashboard
from harness.executor import execute_case
from harness.fixtures import clean_registry, require_registry_schema
from harness.golden import (
    DEFAULT_MIN_EVIDENCE_RECALL,
    GoldenCase,
    GoldenReport,
    aggregate,
    load_golden_cases,
)
from harness.golden_exec import execute_golden_case
from harness.judge import cross_domain_recall_lift
from harness.judge_fixture import (
    DETERMINISTIC_PAIRS,
    JUDGED_EDGES,
)
from harness.judge_fixture import (
    EXPECTED_RELATIONS as JUDGE_EXPECTED_RELATIONS,
)
from harness.metrics import compute_metrics
from harness.records import RunRecord
from harness.report import build_report, render_table, write_report
from harness.run_status import exit_code

EVALS_DIR = Path(__file__).resolve().parent
BASELINE_PATH = EVALS_DIR / "baseline.json"
REPORT_PATH = EVALS_DIR / "report.json"
GOLDEN_DIR = EVALS_DIR / "retrieval_cases" / "golden"


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=EVALS_DIR,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def _load_all_cases(cases_dir: Path | None) -> list[EvalCase]:
    directories = (
        [cases_dir]
        if cases_dir is not None
        else [EVALS_DIR / "retrieval_cases", EVALS_DIR / "agent_task_cases"]
    )
    cases: list[EvalCase] = []
    for directory in directories:
        cases += load_cases(directory)
    if not cases:
        raise SystemExit(f"no cases found in {[str(d) for d in directories]}")
    if cases_dir is None:
        covered = {case.task_type for case in cases}
        missing = [t for t in BENCHMARK_TASK_TYPES if t not in covered]
        if missing:
            raise SystemExit(f"benchmark task types without a case: {missing}")
    return cases


def _load_golden_cases() -> list[GoldenCase]:
    if not GOLDEN_DIR.exists():
        return []
    return load_golden_cases(GOLDEN_DIR)


async def _run_cases(
    cases: list[EvalCase], golden: list[GoldenCase], database_url: str
) -> tuple[list[RunRecord], GoldenReport]:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await require_registry_schema(session)
        records: list[RunRecord] = []
        try:
            for case in cases:
                result = await execute_case(case, factory)
                records.append(result.record)
            # Golden-execution pass (publish gate): drive each golden case through the
            # broker and score evidence-recall + ACL leaks via harness.golden.aggregate
            # (the pinned metric functions). Previously these cases were only loaded —
            # now they actually run, so the floor is enforced (Evals-F8).
            golden_report = aggregate([await execute_golden_case(case, factory) for case in golden])
        finally:
            # the registry is shared with the services' test suites — leave it empty
            async with factory() as session:
                await clean_registry(session)
        return records, golden_report
    finally:
        await engine.dispose()


def _golden_gate_failures(report: GoldenReport) -> list[str]:
    """Publish-gate failures (publish-gates.md): any case under its evidence-recall
    floor (default >= 0.95) or any ACL leak. Empty ⇒ the gate passes."""
    failures: list[str] = []
    if report.cases_below_floor:
        failures.append(
            f"evidence-recall below floor (>= {DEFAULT_MIN_EVIDENCE_RECALL}): "
            f"{', '.join(report.cases_below_floor)}"
        )
    if report.total_acl_leaks > 0:
        failures.append(f"ACL leaks in golden set: {report.total_acl_leaks}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-dir", type=Path, default=None)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the full report JSON")
    parser.add_argument(
        "--fail-on-regress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="exit nonzero (3) when the baseline verdict is 'regressed' "
        "(default: on; always skipped with --update-baseline)",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="render the read-only operator dashboard (ADR-0014 Phase 1) and exit; "
        "reads DATABASE_URL (safe: SELECT-only) or TEST_DATABASE_URL",
    )
    parser.add_argument(
        "--dashboard-out",
        type=Path,
        default=EVALS_DIR,
        help="directory for dashboard.html + dashboard.md (default: evals/)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="ts=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
    )

    if args.dashboard:
        # Unlike the harness below, the dashboard is read-only (SELECTs over the
        # ADR-0014 views), so a real registry via DATABASE_URL is allowed here.
        dashboard_url = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
        if not dashboard_url:
            print(
                "set DATABASE_URL (a registry with the dashboard views migrated) or "
                "TEST_DATABASE_URL to render the dashboard",
                file=sys.stderr,
            )
            return 2
        html_path, md_path = asyncio.run(
            generate_dashboard(dashboard_url, args.dashboard_out, report_path=REPORT_PATH)
        )
        print(f"dashboard written: {html_path} {md_path}")
        return 0

    # TEST_DATABASE_URL only — no DATABASE_URL fallback: the harness seeds and
    # DELETEs registry tables, which must never point at a real registry
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        print(
            "TEST_DATABASE_URL is not set; the harness needs a migrated local registry "
            "(make migrate-test-db). Local runs never require Azure.",
            file=sys.stderr,
        )
        return 2

    cases = _load_all_cases(args.cases_dir)
    # The golden set (evidence-recall publish gate, docs/contracts/golden-query-evals.md)
    # is loaded and surfaced here. Phase 1 reports the set so a missing/duplicate case is
    # caught; the evidence-recall metric is computed by harness.golden over broker results.
    golden = _load_golden_cases()
    records, golden_report = asyncio.run(_run_cases(cases, golden, database_url))

    metrics = compute_metrics(records)
    git_sha = _git_sha()
    comparison = compare(load_baseline(BASELINE_PATH), metrics)
    report = build_report(records, metrics, comparison, git_sha, golden_report)
    write_report(REPORT_PATH, report)

    golden_failures = _golden_gate_failures(golden_report)

    if args.update_baseline:
        write_baseline(BASELINE_PATH, metrics, git_sha)
        print(f"baseline updated: {BASELINE_PATH}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_table(records, metrics, comparison))
        intents = sorted({case.intent for case in golden})
        print(
            f"golden queries: {golden_report.cases} executed (evidence-recall floor "
            f"{DEFAULT_MIN_EVIDENCE_RECALL}) "
            f"mean={_fmt(golden_report.mean_evidence_recall)} "
            f"min={_fmt(golden_report.min_evidence_recall)} "
            f"acl_leaks={golden_report.total_acl_leaks} intents={intents}"
        )
        # Phase-3A candidate quality (docs/contracts/relationship-candidates.md): recall vs
        # the cross-domain golden expectations, sampled precision, volume per artifact, and
        # cost-if-judged — the inputs to the phase-3B affordability decision. NO LLM, no edges.
        cand = aggregate_candidates(GENERATED_CANDIDATES, EXPECTED_RELATIONS)
        print(
            "candidate generator (phase 3A): "
            f"recall={_fmt(cand.recall)} precision={_fmt(cand.precision)} "
            f"volume_per_artifact={_fmt(cand.volume_per_artifact)} "
            f"candidates={cand.candidate_count} "
            f"cost_if_judged_tokens={cand.cost_if_judged_tokens}"
        )
        # Phase-3B judge quality (docs/contracts/relationship-judgment.md): inferred-edge
        # precision + cross-domain evidence-recall LIFT over the deterministic-only
        # baseline. INFERRED edges are routing hints (never claim support), so the win is
        # reaching cross-domain evidence the deterministic linker missed without dropping
        # precision below the gate.
        judge = cross_domain_recall_lift(
            deterministic_pairs=DETERMINISTIC_PAIRS,
            inferred_edges=JUDGED_EDGES,
            expected_relations=JUDGE_EXPECTED_RELATIONS,
        )
        print(
            "relationship judge (phase 3B): "
            f"inferred_edges={judge.inferred_edges} "
            f"inferred_edge_precision={_fmt(judge.inferred_edge_precision)} "
            f"deterministic_recall={_fmt(judge.deterministic_recall)} "
            f"combined_recall={_fmt(judge.combined_recall)} "
            f"recall_lift={_fmt(judge.recall_lift)}"
        )

    # The golden publish gate fails the run (exit 1) on a sub-floor evidence-recall
    # case or any ACL leak — the same severity as a failed benchmark case. Skipped
    # only when no golden cases were loaded (nothing to gate). It is evaluated
    # alongside the benchmark-case + regression gates; the most severe wins.
    code = exit_code(
        records,
        comparison,
        fail_on_regress=args.fail_on_regress,
        update_baseline=args.update_baseline,
    )
    if golden_failures and not args.update_baseline:
        for failure in golden_failures:
            print(f"golden publish gate FAILED: {failure}", file=sys.stderr)
        # exit 1 (case failure severity) unless a benchmark case already failed.
        return code or 1
    return code


if __name__ == "__main__":
    sys.exit(main())
