"""Hermetic tests for codeskeleton — deterministic code compression."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ast

import codeskeleton

_PY = '''\
import os
from typing import Any

CONST = 42


class Widget:
    """A widget."""

    def __init__(self, x: int) -> None:
        self.x = x
        self.y = x * 2
        self.z = x * 3

    def compute(self, factor: int) -> int:
        """Return x scaled by factor."""
        total = 0
        for i in range(factor):
            total += self.x * i
        return total


def top_level(a: str, b: str) -> str:
    result = a + b
    result = result.upper()
    return result
'''


def test_python_keeps_structure_collapses_bodies() -> None:
    r = codeskeleton.skeletonize(_PY, filename="widget.py")
    assert r.method == "python-ast"
    text = r.text
    # imports, constant, class header, all signatures survive
    assert "import os" in text
    assert "from typing import Any" in text
    assert "CONST = 42" in text
    assert "class Widget:" in text
    assert "def __init__(self, x: int) -> None:" in text
    assert "def compute(self, factor: int) -> int:" in text
    assert "def top_level(a: str, b: str) -> str:" in text
    # docstring first line kept; bodies collapsed
    assert '"""Return x scaled by factor."""' in text
    assert "elided" in text
    # body lines are gone
    assert "total += self.x * i" not in text
    assert "result = result.upper()" not in text
    # and it is actually smaller
    assert r.skeleton_tokens < r.original_tokens
    assert r.saved_pct > 0


def test_python_skeleton_still_parses() -> None:
    # the elision placeholder is a comment, so the skeleton must remain valid Python
    r = codeskeleton.skeletonize(_PY, filename="widget.py")
    ast.parse(r.text)  # raises on failure


def test_syntax_error_falls_back_to_line_heuristic() -> None:
    broken = "def f(:\n    this is not valid python\n    x = 1\n"
    r = codeskeleton.skeletonize(broken, filename="broken.py")
    assert r.method == "line-heuristic"
    assert "def f(:" in r.text  # structural line kept even though unparseable


def test_non_python_uses_line_heuristic() -> None:
    js = """\
import { thing } from "./mod";

export function add(a, b) {
    const sum = a + b;
    const doubled = sum * 2;
    const tripled = sum * 3;
    return doubled + tripled;
}
"""
    r = codeskeleton.skeletonize(js, filename="mod.js")
    assert r.method == "line-heuristic"
    assert "export function add(a, b) {" in r.text
    assert "import { thing }" in r.text
    assert "const tripled = sum * 3;" not in r.text  # deep body line collapsed
    assert "elided" in r.text
    assert r.skeleton_tokens < r.original_tokens


def test_empty_and_tiny_do_not_crash() -> None:
    assert codeskeleton.skeletonize("", filename="x.py").text == ""
    one = codeskeleton.skeletonize("x = 1\n", filename="x.py")
    assert "x = 1" in one.text


def test_no_functions_returns_source_unchanged() -> None:
    src = "A = 1\nB = 2\nC = A + B\n"
    r = codeskeleton.skeletonize(src, filename="consts.py")
    assert r.text.strip() == src.strip()


def test_deterministic() -> None:
    a = codeskeleton.skeletonize(_PY, filename="widget.py").text
    b = codeskeleton.skeletonize(_PY, filename="widget.py").text
    assert a == b


def test_multiline_docstring_kept_body_collapsed() -> None:
    src = '''\
def f(a: int) -> int:
    """First line of docstring.

    More detail across several lines that we keep as high-signal intent.
    """
    x = a + 1
    y = x + 1
    return y
'''
    r = codeskeleton.skeletonize(src, filename="m.py")
    assert "def f(a: int) -> int:" in r.text
    assert "First line of docstring." in r.text
    assert "More detail across several lines" in r.text  # whole docstring kept
    assert "x = a + 1" not in r.text  # body collapsed
    assert "elided" in r.text
    ast.parse(r.text)


def test_one_liner_keeps_signature() -> None:
    # regression: `def f(): return x` puts body on the def line — collapsing must NOT eat the def
    src = "def f():\n    return 1\n\n\ndef one(): return 1\n"
    r = codeskeleton.skeletonize(src, filename="o.py")
    assert "def one(): return 1" in r.text  # one-liner kept whole, signature intact
    assert "def f():" in r.text
    ast.parse(r.text)


def test_async_def_collapsed() -> None:
    src = "async def fetch(u: str) -> bytes:\n    a = 1\n    b = 2\n    return b\n"
    r = codeskeleton.skeletonize(src, filename="a.py")
    assert "async def fetch(u: str) -> bytes:" in r.text
    assert "a = 1" not in r.text
    assert "elided" in r.text
    ast.parse(r.text)


def test_nested_functions_single_placeholder() -> None:
    src = (
        "def outer(n: int) -> int:\n"
        "    def inner(k: int) -> int:\n"
        "        return k * 2\n"
        "    total = 0\n"
        "    for i in range(n):\n"
        "        total += inner(i)\n"
        "    return total\n"
    )
    r = codeskeleton.skeletonize(src, filename="n.py")
    assert "def outer(n: int) -> int:" in r.text
    assert "def inner" not in r.text  # nested body collapsed into the outer placeholder
    assert r.text.count("elided") == 1  # one placeholder for the whole outer body
    ast.parse(r.text)


def test_reversibility_contract_original_untouched() -> None:
    # skeletonize must not mutate its input; the caller keeps the exact original for citations
    src = _PY
    codeskeleton.skeletonize(src, filename="widget.py")
    assert src == _PY
