"""Regression tests for kb_agent's safety/budget invariants (ADR-0025 §4, ADR-0026).

Covers the path-traversal sandbox (_safe), the read truncation marker (_truncate), and the
KB-budget predicate (_kb_budget_open) — the one enforced restriction ADR-0025 names non-negotiable.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kb_agent


def test_safe_allows_paths_inside_repo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kb_agent, "REPO_ROOT", tmp_path.resolve())
    assert kb_agent._safe("a/b.py") == (tmp_path / "a/b.py").resolve()
    assert kb_agent._safe(".") == tmp_path.resolve()  # the root itself is allowed


def test_safe_rejects_parent_escape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kb_agent, "REPO_ROOT", tmp_path.resolve())
    with pytest.raises(ValueError, match="escapes REPO_ROOT"):
        kb_agent._safe("../../etc/passwd")


def test_safe_rejects_sibling_prefix_escape(tmp_path: Path, monkeypatch) -> None:
    # the bug a string-prefix check missed: a sibling dir whose name STARTS WITH the repo name.
    root = tmp_path / "repo"
    root.mkdir()
    sibling = tmp_path / "repo-secrets"
    sibling.mkdir()
    (sibling / "creds").write_text("SECRET")
    monkeypatch.setattr(kb_agent, "REPO_ROOT", root.resolve())
    with pytest.raises(ValueError, match="escapes REPO_ROOT"):
        kb_agent._safe("../repo-secrets/creds")


def test_truncate_marks_the_cut() -> None:
    out = kb_agent._truncate("x" * 100, 10)
    assert out.startswith("x" * 10)
    assert "truncated at 10 chars" in out


def test_truncate_leaves_short_input_untouched() -> None:
    assert kb_agent._truncate("short", 100) == "short"


def test_kb_budget_open_requires_both_caps() -> None:
    assert kb_agent._kb_budget_open(2, 500) is True
    assert kb_agent._kb_budget_open(0, 500) is False  # call count exhausted
    assert kb_agent._kb_budget_open(2, 0) is False  # token budget exhausted
    assert kb_agent._kb_budget_open(0, 0) is False
    assert kb_agent._kb_budget_open(-1, 500) is False  # a parallel burst drove it negative
