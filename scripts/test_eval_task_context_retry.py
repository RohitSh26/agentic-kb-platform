"""A/B accounting: `_run_arm` must distinguish a RECOVERED provider-400 retry
(kb_agent._model_step already retried internally and succeeded) from an EXHAUSTED
one (both attempts failed -- today's early-arm-end, unchanged). See
docs/reports/evaluation-2026-07-05.md's 3-6-flakes/20 baseline this counter is
meant to report recovery rate against.

`_model_step` itself is stubbed here (its own retry mechanics are pinned by
test_kb_agent_model_step_retry.py) so this file tests only the seam: how
`_run_arm` turns `_model_step`'s outcome into `retries_recovered` / `model_error`.
The `raw` arm is used throughout so no database or `get_task_context` call is
ever reached.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_task_context
import kb_agent

# Explicit marker (not relying on an ini-level asyncio_mode=auto): this file's test
# path is resolved outside the invoking project's directory tree (repo-root-relative
# `../../scripts/...`), which changes pytest's rootdir/inifile discovery and can leave
# the invoking project's `[tool.pytest.ini_options]` unapplied.
pytestmark = pytest.mark.asyncio


@dataclass
class _FakeCase:
    task: str = "do a thing"
    expected_files: list[str] = field(default_factory=lambda: ["scripts/kb_agent.py"])


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kb_agent, "_make_client", lambda: (object(), "openai", "fake-model"))


async def test_a_recovered_retry_counts_as_recovered_not_a_flake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_model_step(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        native = {"role": "assistant", "content": "FILES: scripts/kb_agent.py"}
        return (native, "FILES: scripts/kb_agent.py", [], 10, 5, True)

    monkeypatch.setattr(kb_agent, "_model_step", fake_model_step)
    result = await eval_task_context._run_arm(_FakeCase(), "raw")
    assert result["retries_recovered"] == 1
    assert result["model_error"] == ""  # a recovery is NOT a flake
    assert result["coverage"] == 1.0  # the arm completed normally, not early-exited


async def test_an_exhausted_retry_still_counts_as_a_flake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_model_step(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        raise RuntimeError("second 400: still invalid")

    monkeypatch.setattr(kb_agent, "_model_step", fake_model_step)
    result = await eval_task_context._run_arm(_FakeCase(), "raw")
    assert result["retries_recovered"] == 0
    assert result["model_error"] == "RuntimeError: second 400: still invalid"  # today's shape
    assert result["coverage"] == 0.0  # the arm ended early, exactly as before


async def test_no_retry_needed_reports_zero_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_model_step(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        native = {"role": "assistant", "content": "FILES: scripts/kb_agent.py"}
        return (native, "FILES: scripts/kb_agent.py", [], 10, 5, False)

    monkeypatch.setattr(kb_agent, "_model_step", fake_model_step)
    result = await eval_task_context._run_arm(_FakeCase(), "raw")
    assert result["retries_recovered"] == 0
    assert result["model_error"] == ""
