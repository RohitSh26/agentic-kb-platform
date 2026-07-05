"""Tier-execution glue (harness.tiers): skip-with-reason when a precondition is missing, and
correct CheckResult translation when a (faked) subprocess/probe actually runs. No real subprocess,
database, or LLM call happens in this file — every I/O seam is injected.

The "ambient environment" regression tests at the bottom pin the 2026-07-05 fork-bomb fix: a tier
function given database_url=None must SKIP — never fall back to os.environ — even when the
matching variable IS set in the ambient environment (harness.tiers module docstring, rule 1).
"""

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from harness.task_context_latency import LatencyResult
from harness.tiers import INNER_PYTEST_GUARD, run_t0, run_t1, run_t2, run_t3, run_t4

EVALS_DIR = Path(__file__).resolve().parent.parent


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _runner_returning(
    result: subprocess.CompletedProcess[str],
) -> Callable[..., "subprocess.CompletedProcess[str]"]:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return result

    return runner


def _runner_by_cmd_substring(
    responses: dict[str, subprocess.CompletedProcess[str]],
) -> Callable[..., "subprocess.CompletedProcess[str]"]:
    def runner(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        for needle, result in responses.items():
            if needle in joined:
                return result
        raise AssertionError(f"no fake response registered for cmd: {cmd}")

    return runner


# ---------------------------------------------------------------------------------------- T0
def test_t0_skips_with_reason_when_gates_not_enabled() -> None:
    tier = run_t0(EVALS_DIR, enabled=False)
    assert tier.status == "skip"
    (check,) = tier.checks
    assert check.status == "skip"
    assert "--with-gates" in (check.reason or "")


def test_t0_passes_when_make_verify_exits_zero() -> None:
    tier = run_t0(EVALS_DIR, enabled=True, runner=_runner_returning(_proc(0)))
    assert tier.status == "pass"


def test_t0_fails_verbatim_when_make_verify_exits_nonzero() -> None:
    tier = run_t0(
        EVALS_DIR,
        enabled=True,
        runner=_runner_returning(_proc(1, stdout="ruff: E501 line too long")),
    )
    assert tier.status == "fail"
    (check,) = tier.checks
    assert check.reason == "exit 1"
    assert "E501" in (check.detail or "")


# ---------------------------------------------------------------------------------------- T1
def test_t1_skips_both_checks_with_the_same_reason_when_test_database_url_is_absent() -> None:
    tier = run_t1(EVALS_DIR, database_url=None)
    assert tier.status == "skip"
    assert len(tier.checks) == 2
    assert all(check.status == "skip" for check in tier.checks)
    assert all("TEST_DATABASE_URL" in (check.reason or "") for check in tier.checks)


def test_t1_reports_run_py_metrics_from_report_json_on_success(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "cases": [{"id": "a", "succeeded": True}],
                "golden": {"cases": 5, "mean_evidence_recall": 1.0, "total_acl_leaks": 0},
                "baseline": {"verdict": "flat"},
            }
        ),
        encoding="utf-8",
    )
    runner = _runner_by_cmd_substring(
        {
            "run.py": _proc(0),
            "pytest": _proc(0, stdout="120 passed in 2.42s"),
        }
    )
    tier = run_t1(tmp_path, database_url="postgresql+asyncpg://x/y", runner=runner)
    assert tier.status == "pass"
    run_py_check, pytest_check = tier.checks
    assert run_py_check.metrics["golden_cases"] == "5"
    assert run_py_check.metrics["baseline_verdict"] == "flat"
    assert pytest_check.metrics["summary"] == "120 passed in 2.42s"


def test_t1_fails_verbatim_when_run_py_exits_nonzero(tmp_path: Path) -> None:
    runner = _runner_by_cmd_substring(
        {
            "run.py": _proc(1, stderr="golden publish gate FAILED: ACL leaks in golden set: 1"),
            "pytest": _proc(0),
        }
    )
    tier = run_t1(tmp_path, database_url="postgresql+asyncpg://x/y", runner=runner)
    assert tier.status == "fail"
    run_py_check = tier.checks[0]
    assert run_py_check.reason is not None and "exit 1" in run_py_check.reason
    assert "ACL leaks" in (run_py_check.detail or "")


# ---------------------------------------------------------------------------------------- T2
def test_t2_skips_both_checks_with_the_same_reason_when_database_url_is_absent(
    tmp_path: Path,
) -> None:
    tier = run_t2(tmp_path, tmp_path, database_url=None)
    assert tier.status == "skip"
    assert len(tier.checks) == 2
    assert all("DATABASE_URL" in (check.reason or "") for check in tier.checks)


async def _fake_latency_probe(database_url: str, tasks: Sequence[str]) -> list[LatencyResult]:
    assert database_url == "postgresql+asyncpg://real/kb"
    return [LatencyResult(task=t, seconds=0.5) for t in tasks]


def test_t2_reports_alias_and_latency_metrics_on_success(tmp_path: Path) -> None:
    runner = _runner_returning(_proc(0, stdout="25/25 top-1 hits = 100.0%\nPASS (target >= 80%)\n"))
    tier = run_t2(
        EVALS_DIR,
        tmp_path,
        database_url="postgresql+asyncpg://real/kb",
        runner=runner,
        latency_probe=_fake_latency_probe,
    )
    assert tier.status == "pass"
    alias_check, latency_check = tier.checks
    assert alias_check.metrics["top1_accuracy_pct"] == "100.0"
    assert latency_check.metrics["n"] == "10"  # task_context_ab_v1.yaml has 10 cases
    assert latency_check.metrics["errors"] == "0"


def test_t2_alias_check_fails_verbatim_on_nonzero_exit(tmp_path: Path) -> None:
    runner = _runner_returning(_proc(1, stderr="no live alias_reference rows found"))
    tier = run_t2(
        EVALS_DIR,
        tmp_path,
        database_url="postgresql+asyncpg://real/kb",
        runner=runner,
        latency_probe=_fake_latency_probe,
    )
    alias_check = tier.checks[0]
    assert alias_check.status == "fail"
    assert "no live alias_reference rows found" in (alias_check.detail or "")


async def _all_errors_probe(database_url: str, tasks: Sequence[str]) -> list[LatencyResult]:
    return [
        LatencyResult(task=t, seconds=None, error="OperationalError: could not connect")
        for t in tasks
    ]


def test_t2_latency_check_fails_when_every_call_errors(tmp_path: Path) -> None:
    runner = _runner_returning(_proc(0, stdout="25/25 top-1 hits = 100.0%\nPASS (target >= 80%)\n"))
    tier = run_t2(
        EVALS_DIR,
        tmp_path,
        database_url="postgresql+asyncpg://real/kb",
        runner=runner,
        latency_probe=_all_errors_probe,
    )
    _, latency_check = tier.checks
    assert latency_check.status == "fail"
    assert "OperationalError" in (latency_check.detail or "")


# ---------------------------------------------------------------------------------------- T3
def test_t3_skips_with_both_reasons_when_nothing_is_configured(tmp_path: Path) -> None:
    tier = run_t3(tmp_path, database_url=None, env={})
    assert tier.status == "skip"
    (check,) = tier.checks
    assert "DATABASE_URL" in (check.reason or "")
    assert "LLM creds" in (check.reason or "")


def test_t3_skips_with_only_the_missing_precondition_when_db_is_present() -> None:
    tier = run_t3(Path("/repo"), database_url="postgresql+asyncpg://x/y", env={})
    (check,) = tier.checks
    assert check.status == "skip"
    assert "DATABASE_URL" not in (check.reason or "")
    assert "LLM creds" in (check.reason or "")


def test_t3_runs_and_parses_the_aggregate_block_when_configured(tmp_path: Path) -> None:
    stdout = (
        "=== AGGREGATE (mean over cases) ===\n"
        "   tooled  coverage=0.400 tool_cover=0.400 steps=1.7 reads=2.7 tokens=2245\n"
        "   raw     coverage=0.000 tool_cover=0.000 steps=2.0 reads=4.2 tokens=1893\n"
    )
    tier = run_t3(
        tmp_path,
        database_url="postgresql+asyncpg://x/y",
        env={"GROQ_API_KEY": "test-key"},
        runner=_runner_returning(_proc(0, stdout=stdout)),
    )
    assert tier.status == "pass"
    (check,) = tier.checks
    assert check.metrics["tooled_coverage"] == "0.400"
    assert check.metrics["raw_coverage"] == "0.000"


# ---------------------------------------------------------------------------------------- T4
def test_t4_is_a_documented_skip_never_a_failure() -> None:
    tier = run_t4()
    assert tier.status == "skip"
    (check,) = tier.checks
    assert "ADR-0008" in (check.reason or "")
    assert check.metrics  # the file inventory is surfaced, not just a bare reason


# ------------------------------------------------- ambient environment (fork-bomb regression)
def _exploding_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError(
        "a tier with database_url=None must never spawn a subprocess "
        "(2026-07-05 fork bomb: None once meant 'read os.environ')"
    )


def test_t1_with_no_database_url_never_reads_the_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://ambient/must-be-ignored")
    tier = run_t1(EVALS_DIR, database_url=None, runner=_exploding_runner)
    assert tier.status == "skip"


def test_t2_with_no_database_url_never_reads_the_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ambient/must-be-ignored")
    tier = run_t2(EVALS_DIR, EVALS_DIR, database_url=None, runner=_exploding_runner)
    assert tier.status == "skip"


def test_t3_with_no_database_url_never_reads_the_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ambient/must-be-ignored")
    monkeypatch.setenv("GROQ_API_KEY", "ambient-key-must-be-ignored")
    tier = run_t3(EVALS_DIR, database_url=None, env={}, runner=_exploding_runner)
    assert tier.status == "skip"


def test_t1_pytest_spawn_carries_the_inner_guard() -> None:
    """The pytest child T1 spawns must carry INNER_PYTEST_GUARD=1 so the runner's own tests
    skip inside it — the second half of the fork-bomb fix (tiers docstring, rule 2)."""
    captured_envs: list[dict[str, str]] = []

    def capturing_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        if "pytest" in " ".join(cmd):
            captured_envs.append(dict(env))
        return _proc(0)

    run_t1(EVALS_DIR, database_url="postgresql+asyncpg://x/y", runner=capturing_runner)
    assert len(captured_envs) == 1
    assert captured_envs[0].get(INNER_PYTEST_GUARD) == "1"
