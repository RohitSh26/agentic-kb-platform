"""Regression test for edit_file's overwrite guardrail (the ADR-test footgun)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kb_agent


def test_empty_old_str_creates_new_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kb_agent, "REPO_ROOT", tmp_path.resolve())
    result = kb_agent.edit_file("new.py", "", "print('hi')\n")
    assert result.startswith("created")
    assert (tmp_path / "new.py").read_text() == "print('hi')\n"


def test_empty_old_str_refuses_to_overwrite_existing_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kb_agent, "REPO_ROOT", tmp_path.resolve())
    (tmp_path / "keep.py").write_text("important = 1\n")
    result = kb_agent.edit_file("keep.py", "", "print('wiped')\n")
    assert result.startswith("error")  # the guardrail
    assert (tmp_path / "keep.py").read_text() == "important = 1\n"  # untouched


def test_anchored_edit_inserts_without_clobbering(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kb_agent, "REPO_ROOT", tmp_path.resolve())
    (tmp_path / "m.py").write_text("a = 1\nb = 2\n")
    result = kb_agent.edit_file("m.py", "b = 2\n", "b = 2\nc = 3\n")
    assert result.startswith("edited")
    assert (tmp_path / "m.py").read_text() == "a = 1\nb = 2\nc = 3\n"
