"""The build-time code-skeleton compressor (ADR-0033), hermetic — no DB, no LLM.

Covers the grounded PR-42 decision: Python files skeletonize deterministically
(signatures + decorators + docstrings kept, bodies elided), other languages pass
through unchanged (no skeleton stored), unparseable Python falls back to the line
heuristic — plus a PRINTED before/after character/token measure on real source
files from this service (run with `-s` to see it).
"""

import ast
from pathlib import Path

import pytest

from agentic_kb_builder.graphify.code_skeleton import (
    estimate_tokens,
    file_skeleton,
    skeletonize,
    skeletonize_python,
)

PYTHON_SOURCE = '''\
import os


@retry(times=3)
def fetch(url: str, timeout: int = 30) -> str:
    """Fetch a URL with retries."""
    attempts = 0
    while attempts < 3:
        attempts += 1
        response = os.popen(f"curl {url}").read()
        if response:
            return response
    raise RuntimeError("unreachable")


class Client:
    """A tiny client."""

    def get(self, path: str) -> str:
        return fetch(path)
'''


class TestPythonSkeleton:
    def test_bodies_elided_structure_kept(self) -> None:
        skeleton = skeletonize_python(PYTHON_SOURCE)
        assert skeleton is not None
        # structure survives: imports, decorator, signatures, docstrings
        assert "import os" in skeleton
        assert "@retry(times=3)" in skeleton
        assert "def fetch(url: str, timeout: int = 30) -> str:" in skeleton
        assert '"""Fetch a URL with retries."""' in skeleton
        assert "class Client:" in skeleton
        assert "def get(self, path: str) -> str:" in skeleton
        # bodies are gone, replaced by counted placeholders
        assert "attempts" not in skeleton
        assert "return fetch(path)" not in skeleton
        assert "lines elided" in skeleton

    def test_skeleton_is_valid_python_and_deterministic(self) -> None:
        first = skeletonize_python(PYTHON_SOURCE)
        second = skeletonize_python(PYTHON_SOURCE)
        assert first == second  # same input => same output, pure rules
        assert first is not None
        ast.parse(first)  # the skeleton stays parseable Python

    def test_one_liner_def_is_kept_whole(self) -> None:
        # the body sits on the def line itself: nothing to collapse, source returned intact
        source = "def f(): return 1\n"
        assert skeletonize_python(source) == source

    def test_unparseable_python_falls_back_to_the_line_heuristic(self) -> None:
        broken = "def broken(:\n    x = 1\n"
        result = skeletonize(broken, filename="broken.py")
        assert result.method == "line-heuristic"


class TestFileSkeletonGate:
    def test_python_file_is_skeletonized(self) -> None:
        result = file_skeleton(PYTHON_SOURCE, path="pkg/client.py")
        assert result is not None
        assert result.method == "python-ast"
        assert result.skeleton_tokens < result.original_tokens

    @pytest.mark.parametrize("path", ["app.ts", "main.go", "Service.java", "README.md"])
    def test_other_languages_pass_through_unchanged(self, path: str) -> None:
        # No skeleton is stored for non-Python sources: they pass through the
        # build unchanged (search_text stays None) — never a stored raw document.
        assert file_skeleton("function f() { return 1; }", path=path) is None

    def test_pyi_stubs_take_the_python_path(self) -> None:
        result = file_skeleton("def f() -> int: ...\n", path="pkg/stubs.pyi")
        assert result is not None


class TestPrintedMeasureOnRealFiles:
    def test_savings_on_real_service_source_files(self) -> None:
        """The ADR-0026/0033 measure, recomputed on THIS service's real files and
        printed (character + token before/after). Asserts the direction, prints
        the magnitude — honest measurement, not a hardcoded number."""
        src_root = Path(__file__).resolve().parents[2] / "src" / "agentic_kb_builder"
        files = sorted(src_root.rglob("*.py"))
        assert files, "kb-builder source files must exist"
        total_chars = total_skeleton_chars = 0
        total_tokens = total_skeleton_tokens = 0
        for path in files:
            source = path.read_text(encoding="utf-8")
            result = file_skeleton(source, path=str(path))
            assert result is not None
            total_chars += len(source)
            total_skeleton_chars += len(result.text)
            total_tokens += result.original_tokens
            total_skeleton_tokens += result.skeleton_tokens
        saved_pct = 100.0 * (total_tokens - total_skeleton_tokens) / total_tokens
        print(
            f"\nskeleton savings on {len(files)} real kb-builder files: "
            f"chars {total_chars} -> {total_skeleton_chars}, "
            f"tokens {total_tokens} -> {total_skeleton_tokens} "
            f"({saved_pct:.0f}% saved)"
        )
        # the ADR-0026 corpus measured ~41-45%; assert a conservative floor only
        assert total_skeleton_tokens < total_tokens * 0.8


def test_estimate_tokens_rounds_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
