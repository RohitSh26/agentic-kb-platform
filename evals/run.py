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
from harness.cases import BENCHMARK_TASK_TYPES, EvalCase, load_cases
from harness.executor import execute_case
from harness.fixtures import clean_registry, require_registry_schema
from harness.golden import GoldenCase, load_golden_cases
from harness.metrics import compute_metrics
from harness.records import RunRecord
from harness.report import build_report, render_table, write_report
from harness.run_status import exit_code

EVALS_DIR = Path(__file__).resolve().parent
BASELINE_PATH = EVALS_DIR / "baseline.json"
REPORT_PATH = EVALS_DIR / "report.json"
GOLDEN_DIR = EVALS_DIR / "retrieval_cases" / "golden"


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


async def _run_cases(cases: list[EvalCase], database_url: str) -> list[RunRecord]:
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
        finally:
            # the registry is shared with the services' test suites — leave it empty
            async with factory() as session:
                await clean_registry(session)
        return records
    finally:
        await engine.dispose()


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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="ts=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
    )

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
    records = asyncio.run(_run_cases(cases, database_url))

    metrics = compute_metrics(records)
    git_sha = _git_sha()
    comparison = compare(load_baseline(BASELINE_PATH), metrics)
    report = build_report(records, metrics, comparison, git_sha)
    write_report(REPORT_PATH, report)

    if args.update_baseline:
        write_baseline(BASELINE_PATH, metrics, git_sha)
        print(f"baseline updated: {BASELINE_PATH}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_table(records, metrics, comparison))
        intents = sorted({case.intent for case in golden})
        print(
            f"golden queries: {len(golden)} loaded (evidence-recall floor 0.95) intents={intents}"
        )

    return exit_code(
        records,
        comparison,
        fail_on_regress=args.fail_on_regress,
        update_baseline=args.update_baseline,
    )


if __name__ == "__main__":
    sys.exit(main())
