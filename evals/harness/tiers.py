"""Tier-execution glue for the consolidated runner (evals/run_all.py).

Each `run_tN` function is intentionally thin: it does NOT reimplement any evaluation logic — it
shells out to (or, for the T2 latency probe, calls in-process) the runner/suite that already owns
that tier's logic, and translates the result into a `TierResult`. See
docs/architecture/evaluation-system.md for what each tier is and why it lives where it does.

Every subprocess call takes an injectable `runner` (defaults to `subprocess.run`) so tests can
supply a canned `CompletedProcess` without actually shelling out to `uv`/`pytest`/an LLM — the
same "port" the tier functions accept for `database_url`/`env` so skip-with-reason paths are
testable without touching the environment.
"""

import asyncio
import os
import subprocess
import time
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

from harness.task_context_ab import load_ab_cases
from harness.task_context_latency import LatencyResult, probe, summarize
from harness.tier_parsers import (
    parse_alias_output,
    parse_run_py_report,
    parse_task_context_ab_output,
)
from harness.tier_result import CheckResult, TierResult

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
LatencyProbe = Callable[[str, Sequence[str]], Coroutine[Any, Any, list[LatencyResult]]]

_DETAIL_TAIL_LINES = 40
_DETAIL_MAX_CHARS = 4000


def _tail(text: str) -> str:
    """The last `_DETAIL_TAIL_LINES` lines of `text`, verbatim (never paraphrased), capped in
    length so one runaway process can't blow up the report."""
    stripped = text.strip()
    if not stripped:
        return "(no output captured)"
    lines = stripped.splitlines()[-_DETAIL_TAIL_LINES:]
    joined = "\n".join(lines)
    return joined[-_DETAIL_MAX_CHARS:]


def _run(
    runner: Runner,
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    merged_env = {**os.environ, **(env or {})}
    return runner(
        cmd, cwd=str(cwd), env=merged_env, capture_output=True, text=True, timeout=timeout
    )


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


# --------------------------------------------------------------------------------------- T0
def run_t0(repo_root: Path, *, enabled: bool, runner: Runner = subprocess.run) -> TierResult:
    title = "T0 — hermetic per-project gates (make verify: ruff + pyright + pytest, no creds)"
    if not enabled:
        return TierResult(
            "T0",
            title,
            (
                CheckResult(
                    "make verify",
                    "skip",
                    reason="opt-in: pass --with-gates to run it (three uv-managed services + "
                    "evals; slow, and already CI's job)",
                ),
            ),
        )
    start = time.monotonic()
    proc = _run(runner, ["make", "verify"], cwd=repo_root, timeout=1800)
    duration = time.monotonic() - start
    status = "pass" if proc.returncode == 0 else "fail"
    return TierResult(
        "T0",
        title,
        (
            CheckResult(
                "make verify",
                status,
                reason=None if status == "pass" else f"exit {proc.returncode}",
                detail=None if status == "pass" else _tail(proc.stdout + proc.stderr),
                duration_seconds=duration,
            ),
        ),
    )


# --------------------------------------------------------------------------------------- T1
def run_t1(
    evals_dir: Path, *, database_url: str | None = None, runner: Runner = subprocess.run
) -> TierResult:
    title = (
        "T1 — deterministic golden sets (run.py + hermetic pytest; migrated-but-fixture-seeded "
        "registry, zero LLM)"
    )
    resolved_url = database_url if database_url is not None else os.environ.get("TEST_DATABASE_URL")
    if not resolved_url:
        reason = "TEST_DATABASE_URL not set (needs a migrated registry: make migrate-test-db)"
        return TierResult(
            "T1",
            title,
            (
                CheckResult(
                    "run.py (retrieval + agent-task cases + golden publish gate)",
                    "skip",
                    reason=reason,
                ),
                CheckResult("evals hermetic pytest suite (uv run pytest)", "skip", reason=reason),
            ),
        )

    checks: list[CheckResult] = []

    start = time.monotonic()
    proc = _run(
        runner,
        ["uv", "run", "python", "run.py"],
        cwd=evals_dir,
        env={"TEST_DATABASE_URL": resolved_url},
        timeout=300,
    )
    duration = time.monotonic() - start
    status = "pass" if proc.returncode == 0 else "fail"
    checks.append(
        CheckResult(
            "run.py (retrieval + agent-task cases + golden publish gate)",
            status,
            reason=None
            if status == "pass"
            else f"exit {proc.returncode} (see docs/contracts/evals-report.md)",
            detail=None if status == "pass" else _tail(proc.stdout + proc.stderr),
            metrics=parse_run_py_report(evals_dir / "report.json"),
            duration_seconds=duration,
        )
    )

    start = time.monotonic()
    proc2 = _run(
        runner,
        ["uv", "run", "pytest", "-q"],
        cwd=evals_dir,
        env={"TEST_DATABASE_URL": resolved_url},
        timeout=300,
    )
    duration2 = time.monotonic() - start
    status2 = "pass" if proc2.returncode == 0 else "fail"
    checks.append(
        CheckResult(
            "evals hermetic pytest suite (uv run pytest)",
            status2,
            reason=None if status2 == "pass" else f"exit {proc2.returncode}",
            detail=None if status2 == "pass" else _tail(proc2.stdout + proc2.stderr),
            metrics={"summary": _last_nonempty_line(proc2.stdout)},
            duration_seconds=duration2,
        )
    )
    return TierResult("T1", title, tuple(checks))


def _last_nonempty_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "(no output)"


# --------------------------------------------------------------------------------------- T2
def run_t2(
    evals_dir: Path,
    repo_root: Path,
    *,
    database_url: str | None = None,
    runner: Runner = subprocess.run,
    latency_probe: LatencyProbe | None = None,
) -> TierResult:
    title = "T2 — live-KB deterministic (alias full run + get_task_context latency; zero LLM)"
    resolved_url = database_url if database_url is not None else os.environ.get("DATABASE_URL")
    if not resolved_url:
        reason = (
            "DATABASE_URL not set (needs a locally built KB — "
            "docs/dev-guide/03-local-testing.md 'Running an end-to-end build locally')"
        )
        return TierResult(
            "T2",
            title,
            (
                CheckResult(
                    "alias full run (scripts/eval_alias_resolution.py)", "skip", reason=reason
                ),
                CheckResult("get_task_context latency probe", "skip", reason=reason),
            ),
        )

    checks: list[CheckResult] = []

    start = time.monotonic()
    proc = _run(
        runner,
        ["uv", "run", "python", str(repo_root / "scripts" / "eval_alias_resolution.py")],
        cwd=repo_root / "services" / "kb-builder",
        env={"DATABASE_URL": resolved_url},
        timeout=180,
    )
    duration = time.monotonic() - start
    status = "pass" if proc.returncode == 0 else "fail"
    checks.append(
        CheckResult(
            "alias full run",
            status,
            reason=None if status == "pass" else f"exit {proc.returncode}",
            detail=None if status == "pass" else _tail(proc.stdout + proc.stderr),
            metrics=parse_alias_output(proc.stdout),
            duration_seconds=duration,
        )
    )

    start = time.monotonic()
    try:
        tasks = [
            case.task
            for case in load_ab_cases(evals_dir / "agent_task_cases" / "task_context_ab_v1.yaml")
        ]
        probe_fn = latency_probe or probe
        results = asyncio.run(probe_fn(resolved_url, tasks))
        report = summarize(results)
        duration2 = time.monotonic() - start
        status2 = "pass" if report.p50_seconds is not None else "fail"
        checks.append(
            CheckResult(
                "get_task_context latency probe",
                status2,
                reason=None if status2 == "pass" else "every task errored",
                detail=(
                    None
                    if status2 == "pass"
                    else _tail(
                        "\n".join(f"{r.task}: {r.error}" for r in results if r.error is not None)
                    )
                ),
                metrics={
                    "n": str(report.n),
                    "p50_seconds": _fmt(report.p50_seconds),
                    "p95_seconds": _fmt(report.p95_seconds),
                    "errors": str(len(report.errors)),
                },
                duration_seconds=duration2,
            )
        )
    except Exception as exc:  # a probe crash must still produce a reportable, verbatim row
        checks.append(
            CheckResult(
                "get_task_context latency probe",
                "fail",
                reason=f"{type(exc).__name__}",
                detail=str(exc),
                duration_seconds=time.monotonic() - start,
            )
        )
    return TierResult("T2", title, tuple(checks))


# --------------------------------------------------------------------------------------- T3
def _llm_creds_present(env: dict[str, str]) -> bool:
    return bool(env.get("LLM_API_KEY") or env.get("GROQ_API_KEY"))


def run_t3(
    repo_root: Path,
    *,
    database_url: str | None = None,
    limit: int | None = 3,
    env: dict[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> TierResult:
    title = (
        "T3 — LLM-armed two-arm A/B (get_task_context tooled vs raw; "
        "provider-agnostic via kb_agent's shim)"
    )
    resolved_env = env if env is not None else dict(os.environ)
    resolved_url = database_url if database_url is not None else resolved_env.get("DATABASE_URL")

    reasons: list[str] = []
    if not resolved_url:
        reasons.append("DATABASE_URL not set (needs a locally built KB)")
    if not _llm_creds_present(resolved_env):
        reasons.append("no LLM creds (LLM_API_KEY/GROQ_API_KEY unset)")
    if reasons:
        return TierResult(
            "T3",
            title,
            (
                CheckResult(
                    "two-arm A/B (scripts/eval_task_context.py)", "skip", reason="; ".join(reasons)
                ),
            ),
        )
    assert resolved_url is not None  # narrowed by the reasons check above

    cmd = ["uv", "run", "python", str(repo_root / "scripts" / "eval_task_context.py")]
    if limit is not None:
        cmd += ["--limit", str(limit)]
    start = time.monotonic()
    proc = _run(
        runner,
        cmd,
        cwd=repo_root / "services" / "mcp-server",
        env={"DATABASE_URL": resolved_url},
        timeout=1200,
    )
    duration = time.monotonic() - start
    status = "pass" if proc.returncode == 0 else "fail"
    return TierResult(
        "T3",
        title,
        (
            CheckResult(
                "two-arm A/B (scripts/eval_task_context.py)",
                status,
                reason=None if status == "pass" else f"exit {proc.returncode}",
                detail=None if status == "pass" else _tail(proc.stdout + proc.stderr),
                metrics=parse_task_context_ab_output(proc.stdout),
                duration_seconds=duration,
            ),
        ),
    )


# --------------------------------------------------------------------------------------- T4
def run_t4() -> TierResult:
    title = (
        "T4 — adversarial fixtures (injection, budget-cap, ACL, dev-gate); live in service test "
        "suites by design"
    )
    reason = (
        "not executed directly by this runner — these fixtures need each owning service's real "
        "dependencies (review-panel's LangGraph checkpointer, mcp-server's RBAC/budget/runner "
        "code); duplicating that machinery in evals/ would violate ADR-0008 (self-contained "
        "services) for no benefit. They already run as part of T0 (--with-gates / make verify), "
        "or directly via `make test-mcp-server` / `make test-review-panel`. See "
        "docs/architecture/evaluation-system.md 'T4' for the full rationale."
    )
    inventory = {
        "review-panel (injection)": "tests/integration/test_injection.py",
        "mcp-server (budget)": (
            "tests/unit/test_budgets.py, test_kb_search_budget.py, test_token_budget.py"
        ),
        "mcp-server (acl)": "tests/unit/test_rbac.py, tests/integration/test_security.py",
        "mcp-server (dev-gate)": "tests/unit/test_runner_build_lane.py",
    }
    return TierResult(
        "T4",
        title,
        (
            CheckResult(
                "adversarial fixtures (by design, in service suites)",
                "skip",
                reason=reason,
                metrics=inventory,
            ),
        ),
    )
