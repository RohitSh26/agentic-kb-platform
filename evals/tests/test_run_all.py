"""The consolidated runner's CLI + top-level wiring (evals/run_all.py).

Arg parsing and report rendering are pure and tested directly. The one end-to-end `main()` test
forces every credential/DB env var OFF (monkeypatch) so T1/T2/T3 deterministically skip and no
real subprocess, database, or LLM call ever happens here — T0 is off by default (--with-gates not
passed), and T4 is pure. This keeps the test hermetic regardless of the ambient environment (e.g.
TEST_DATABASE_URL exported by `make test-evals`).

This MODULE skips itself under the T1 inner-pytest guard (harness.tiers.INNER_PYTEST_GUARD):
when run_all.py's T1 spawns evals' pytest suite, re-running the runner's own tests inside that
child could recurse into another T1 pytest spawn (the 2026-07-05 fork bomb — see the
harness.tiers module docstring). The outer run_all execution is itself the coverage for what
these tests pin, so nothing is lost by the skip.
"""

import os
from pathlib import Path

import pytest

import run_all
from harness.consolidated_report import render_markdown
from harness.tier_result import CheckResult, TierResult
from harness.tiers import INNER_PYTEST_GUARD

pytestmark = pytest.mark.skipif(
    os.environ.get(INNER_PYTEST_GUARD) == "1",
    reason="running inside run_all.py's own T1 pytest spawn — the outer run covers these; "
    "re-running them here could recurse (see harness.tiers docstring)",
)


def test_default_tiers_is_all_five() -> None:
    args = run_all.build_parser().parse_args([])
    assert args.tiers == run_all.ALL_TIERS
    assert args.with_gates is False
    assert args.t3_full is False
    assert args.t3_limit == 3
    assert args.out == run_all.DEFAULT_OUT


def test_tiers_flag_accepts_a_comma_separated_subset() -> None:
    args = run_all.build_parser().parse_args(["--tiers", "t1,t2"])
    assert args.tiers == ("t1", "t2")


def test_tiers_flag_lowercases_and_strips_whitespace() -> None:
    args = run_all.build_parser().parse_args(["--tiers", " T1 , T3 "])
    assert args.tiers == ("t1", "t3")


def test_unknown_tier_is_rejected_at_parse_time() -> None:
    with pytest.raises(SystemExit):
        run_all.build_parser().parse_args(["--tiers", "t1,bogus"])


def test_with_gates_and_out_and_t3_full_flags() -> None:
    args = run_all.build_parser().parse_args(["--with-gates", "--t3-full", "--out", "/tmp/x.md"])
    assert args.with_gates is True
    assert args.t3_full is True
    assert args.out == Path("/tmp/x.md")


def test_run_only_executes_the_requested_tiers() -> None:
    # t4 is pure and always available; restricting --tiers to it alone must produce exactly
    # one TierResult, proving the flag actually filters (not just decoration).
    args = run_all.build_parser().parse_args(["--tiers", "t4"])
    tiers, report_md = run_all.run(args)
    assert [tier.tier for tier in tiers] == ["T4"]
    assert "| T4 |" in report_md
    assert "| T0 |" not in report_md  # bare "T0" would false-positive on ISO timestamps like T06:..


def test_main_end_to_end_all_unconfigured_tiers_skip_and_report_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("TEST_DATABASE_URL", "DATABASE_URL", "LLM_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    out_path = tmp_path / "report_all.md"

    exit_status = run_all.main(["--out", str(out_path)])

    assert exit_status == 0  # every tier skipped (or T4's documented skip) — never a failure
    written = out_path.read_text(encoding="utf-8")
    assert "T1" in written and "T2" in written and "T3" in written
    assert "SKIP" in written
    assert "TEST_DATABASE_URL not set" in written
    assert "no LLM creds" in written


# ------------------------------------------------------------------------- report shape (pure)
def test_render_markdown_includes_a_summary_row_per_tier() -> None:
    tiers = [
        TierResult("T1", "golden sets", (CheckResult("run.py", "pass", duration_seconds=1.2),)),
        TierResult("T2", "live KB", (CheckResult("alias", "skip", reason="DATABASE_URL not set"),)),
    ]
    report = render_markdown(tiers, git_sha="abc1234")
    assert "| T1 | PASS |" in report
    assert "| T2 | SKIP |" in report
    assert "abc1234" in report


def test_render_markdown_renders_failure_detail_verbatim_in_a_fenced_block() -> None:
    tiers = [
        TierResult(
            "T1",
            "golden sets",
            (
                CheckResult(
                    "run.py",
                    "fail",
                    reason="exit 1",
                    detail="Traceback (most recent call last):\nValueError: boom",
                ),
            ),
        )
    ]
    report = render_markdown(tiers, git_sha=None)
    assert "```" in report
    assert "ValueError: boom" in report
    assert "**Overall: FAIL**" in report


def test_render_markdown_overall_is_skip_when_every_tier_skips() -> None:
    tiers = [TierResult("T3", "a/b", (CheckResult("ab", "skip", reason="no creds"),))]
    report = render_markdown(tiers, git_sha=None)
    assert "**Overall: SKIP**" in report
