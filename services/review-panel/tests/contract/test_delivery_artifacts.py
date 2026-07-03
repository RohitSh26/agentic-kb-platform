"""Delivery contract (ADR-0031): on-demand CLI + local runner only.

No auto-triggering GitHub Actions workflow exists in v1 — the old
review-panel.yml from the interrupted attempt must stay deleted — and the
repo's verify gating covers this service like the other two.
"""

import os

from panel_test_support import REPO_ROOT

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
LOCAL_RUNNER = REPO_ROOT / "scripts" / "run_review_panel_local.sh"


def test_no_auto_triggering_review_panel_workflow_exists() -> None:
    assert not (WORKFLOWS_DIR / "review-panel.yml").exists()
    assert not (WORKFLOWS_DIR / "review-panel.yaml").exists()


def test_no_workflow_runs_the_panel_on_pull_request_events() -> None:
    """CI may lint/type/test this service, but nothing may EXECUTE the panel
    from a workflow (an optional non-posting precompute is a later decision)."""
    for workflow in WORKFLOWS_DIR.glob("*.y*ml"):
        text = workflow.read_text(encoding="utf-8")
        assert "review-panel draft" not in text, f"{workflow.name} auto-runs the panel"
        assert "python -m review_panel" not in text, f"{workflow.name} auto-runs the panel"


def test_repo_verify_gating_includes_review_panel() -> None:
    ci = (WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8")
    assert "services/review-panel" in ci
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "review-panel" in makefile.splitlines()[0]  # SERVICES := ... review-panel


def test_local_runner_ships_and_wraps_the_draft_cli() -> None:
    assert LOCAL_RUNNER.exists()
    assert os.access(LOCAL_RUNNER, os.X_OK)
    text = LOCAL_RUNNER.read_text(encoding="utf-8")
    assert "review-panel draft" in text
