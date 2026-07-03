"""Frontmatter stripping and manifest loading behavior (pure logic)."""

from pathlib import Path

import pytest

from review_panel.graph.prompts import PromptLoadError, load_panel_prompts, strip_frontmatter


def test_strip_frontmatter_returns_only_the_body() -> None:
    text = "---\nname: x\nversion: 1.0\n---\nYou are the reviewer.\nSecond line."
    assert strip_frontmatter(text) == "You are the reviewer.\nSecond line."


def test_text_without_frontmatter_passes_through() -> None:
    assert strip_frontmatter("Just a body.") == "Just a body."


def test_unclosed_frontmatter_fails_loudly() -> None:
    with pytest.raises(PromptLoadError):
        strip_frontmatter("---\nname: x\nnever closes")


def test_missing_manifest_dir_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(PromptLoadError):
        load_panel_prompts(tmp_path)


def test_empty_body_fails_loudly(tmp_path: Path) -> None:
    for name in (
        "bug_reviewer.md",
        "security_reviewer.md",
        "quality_reviewer.md",
        "test_coverage_reviewer.md",
        "code_reviewer.md",
    ):
        (tmp_path / name).write_text("---\nname: x\n---\n", encoding="utf-8")
    with pytest.raises(PromptLoadError):
        load_panel_prompts(tmp_path)
