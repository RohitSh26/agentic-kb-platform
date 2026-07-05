"""Consolidated evaluation entry point (docs/architecture/evaluation-system.md).

Runs every tier that CAN run in this environment. T1/T2/T3 each detect what they need
(TEST_DATABASE_URL, DATABASE_URL, LLM creds) and SKIP with a stated reason instead of failing or
inventing a number when it's missing. Every tier invokes its OWN existing runner or suite —
run_all.py never reimplements retrieval/agent-quality logic, only orchestrates and reports it.

T0 (generic per-project ruff+pyright+pytest gates, `make verify`) is opt-in via --with-gates: it
already runs in CI and is slow to repeat here (three uv-managed services + evals). T4 (adversarial
fixtures) is never executed here by design — see harness.tiers.run_t4's docstring and
docs/architecture/evaluation-system.md "T4" for why.

Usage (from evals/):
    uv run python run_all.py                     # T1-T4 (T1/T2/T3 auto-skip if unavailable)
    uv run python run_all.py --with-gates         # + T0 (make verify)
    uv run python run_all.py --tiers t1,t2        # only these tiers
    uv run python run_all.py --t3-full            # all T3 cases, not the default 3-case smoke
    uv run python run_all.py --out /tmp/report.md
"""

import argparse
import subprocess
import sys
from pathlib import Path

from harness.consolidated_report import render_markdown
from harness.tier_result import TierResult, exit_code
from harness.tiers import run_t0, run_t1, run_t2, run_t3, run_t4

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
DEFAULT_OUT = EVALS_DIR / "report_all.md"
ALL_TIERS = ("t0", "t1", "t2", "t3", "t4")


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def _parse_tiers(raw: str) -> tuple[str, ...]:
    requested = tuple(tier.strip().lower() for tier in raw.split(",") if tier.strip())
    unknown = set(requested) - set(ALL_TIERS)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown tier(s) {sorted(unknown)}; choose from {ALL_TIERS}"
        )
    return requested


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"markdown report path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--with-gates", action="store_true", help="also run T0 (make verify) — slow, opt-in"
    )
    parser.add_argument(
        "--tiers",
        type=_parse_tiers,
        default=ALL_TIERS,
        help="comma-separated subset of t0,t1,t2,t3,t4",
    )
    parser.add_argument(
        "--t3-limit", type=int, default=3, help="T3 case cap for a bounded smoke run (default 3)"
    )
    parser.add_argument(
        "--t3-full",
        action="store_true",
        help="run all T3 cases (overrides --t3-limit); costs real LLM tokens",
    )
    return parser


def run(args: argparse.Namespace) -> tuple[list[TierResult], str]:
    tiers: list[TierResult] = []
    if "t0" in args.tiers:
        tiers.append(run_t0(REPO_ROOT, enabled=args.with_gates))
    if "t1" in args.tiers:
        tiers.append(run_t1(EVALS_DIR))
    if "t2" in args.tiers:
        tiers.append(run_t2(EVALS_DIR, REPO_ROOT))
    if "t3" in args.tiers:
        tiers.append(run_t3(REPO_ROOT, limit=None if args.t3_full else args.t3_limit))
    if "t4" in args.tiers:
        tiers.append(run_t4())
    report_md = render_markdown(tiers, git_sha=_git_sha())
    return tiers, report_md


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tiers, report_md = run(args)

    print(report_md)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report_md + "\n", encoding="utf-8")
    print(f"\nwrote {args.out}", file=sys.stderr)

    return exit_code(tiers)


if __name__ == "__main__":
    sys.exit(main())
