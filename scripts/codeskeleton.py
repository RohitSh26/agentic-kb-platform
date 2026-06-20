"""codeskeleton — deterministic, reversible code compression (our own "Headroom" idea).

The token sink for a coding agent is reading whole files. But to ORIENT in a codebase — to
write a method that fits — the model mostly needs *structure*: imports, class/function
signatures, type hints, and a one-line purpose. The function *bodies* of neighbouring code are
usually noise for that task. So we compress a source file to its **skeleton**: keep the shape,
collapse the bodies.

This is:
- **Deterministic** — pure AST/line rules, no ML, no network, same input ⇒ same output.
- **Reversible** — we never mutate the original; the full file is always on disk / in the KB.
  The agent opens the skeleton to orient, then pulls the exact body of the 1-2 things it
  actually touches.
- **Lossy by design, for THINKING not CITING** — bodies are dropped from the skeleton. Never
  use a skeleton where exact text matters (a verbatim quote / citation) — read the original.

Python uses the `ast` module (precise). Everything else (and any file that fails to parse) uses
a language-agnostic line heuristic that keeps structural lines and collapses indented bodies.

CLI:
    python codeskeleton.py path/to/file.py        # print the skeleton + savings
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Structural line markers for the non-Python fallback: signatures, declarations, block edges.
# Longest alternatives first so e.g. "async def" is tried before "def".
_STRUCT_RE = re.compile(
    r"^\s*(?:"
    r"async\s+def|def|class|@|"  # python-ish (fallback path)
    r"export\s+default|export|import|from|"  # js/ts
    r"public|private|protected|internal|static|abstract|"  # access modifiers
    r"function|func|fn|"  # js / go / rust
    r"interface|type|struct|enum|impl|trait"  # type decls
    r"|module|package|namespace"  # grouping
    r")\b"
)
# A line that is only a closing brace / paren (kept so block structure stays readable).
_CLOSER_RE = re.compile(r"^\s*[)}\]]+;?\s*$")

# Rough provider-agnostic token estimate. ~4 chars/token is the usual English/code heuristic;
# good enough for measuring relative savings (we report deltas, not absolute billing).
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


@dataclass(frozen=True)
class SkeletonResult:
    text: str
    original_tokens: int
    skeleton_tokens: int
    method: str  # "python-ast" | "line-heuristic"

    @property
    def saved_tokens(self) -> int:
        return self.original_tokens - self.skeleton_tokens

    @property
    def saved_pct(self) -> float:
        if self.original_tokens == 0:
            return 0.0
        return 100.0 * self.saved_tokens / self.original_tokens


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _collapse_intervals(tree: ast.Module) -> list[tuple[int, int]]:
    """1-based (start, end) inclusive line ranges of function/method BODIES to collapse.

    For each function we keep its signature and the first line of its docstring (if any), then
    collapse the rest of the body. Nested functions fall inside their parent's range, so we merge
    overlapping/contained intervals and emit a single placeholder per top-level body.
    """
    raw: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        # if the first statement is a docstring, keep the WHOLE docstring (high-signal intent) and
        # collapse only what follows it; otherwise collapse the whole body
        start = (first.end_lineno or first.lineno) + 1 if _is_docstring(first) else first.lineno
        end = node.end_lineno or start
        # Only collapse lines STRICTLY below the signature. A one-liner like `def f(): return x`
        # puts the body on the def line itself (start == node.lineno); collapsing it would delete
        # the signature too, so we keep such functions whole.
        if start > node.lineno and start <= end:
            raw.append((start, end))

    if not raw:
        return []
    raw.sort(key=lambda iv: (iv[0], -iv[1]))
    merged: list[tuple[int, int]] = [raw[0]]
    for start, end in raw[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # contained / overlapping (nested function) → absorb
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def skeletonize_python(source: str) -> str | None:
    """Return the AST-based skeleton, or None if the source does not parse (caller falls back)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    intervals = _collapse_intervals(tree)
    if not intervals:
        return source  # nothing to collapse (no function bodies)

    lines = source.splitlines()
    # map each interval-start line → placeholder; mark all interval lines as dropped
    starts = {start: end for start, end in intervals}
    dropped: set[int] = {n for start, end in intervals for n in range(start, end + 1)}

    out: list[str] = []
    for lineno in range(1, len(lines) + 1):
        line = lines[lineno - 1]
        if lineno in starts:
            end = starts[lineno]
            indent = len(line) - len(line.lstrip(" "))
            n = end - lineno + 1
            out.append(" " * indent + f"... # {n} line{'s' if n != 1 else ''} elided")
        elif lineno in dropped:
            continue
        else:
            out.append(line)
    return "\n".join(out)


def skeletonize_lines(source: str) -> str:
    """Language-agnostic fallback: keep structural lines, collapse indented body runs.

    A line is kept if it is top-level (indent 0), matches a structural marker (a signature/decl/
    type at any depth), is a bare closing brace, or is blank. Everything else is a body line and
    gets collapsed into a single `... # N lines elided` placeholder per run. Heuristic by nature —
    Python takes the precise AST path; this only runs for other languages or unparseable files.
    """
    lines = source.splitlines()
    out: list[str] = []
    run = 0  # consecutive dropped lines

    def flush() -> None:
        nonlocal run
        if run:
            out.append(f"    ... # {run} line{'s' if run != 1 else ''} elided")
            run = 0

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" \t"))
        keep = (
            not stripped  # blank
            or indent == 0  # top-level statement / declaration
            or bool(_STRUCT_RE.match(line))  # signature / decl / type at any depth
            or bool(_CLOSER_RE.match(line))
        )
        if keep:
            flush()
            out.append(line)
        else:
            run += 1
    flush()
    return "\n".join(out)


_PYTHON_SUFFIXES = {".py", ".pyi"}


def skeletonize(source: str, *, filename: str = "") -> SkeletonResult:
    """Compress source to its skeleton. AST path for Python, line heuristic otherwise."""
    original_tokens = estimate_tokens(source)
    suffix = Path(filename).suffix.lower()

    method = "python-ast"
    text: str | None = None
    if suffix in _PYTHON_SUFFIXES or (not suffix and "def " in source):
        text = skeletonize_python(source)
    if text is None:
        text = skeletonize_lines(source)
        method = "line-heuristic"

    return SkeletonResult(
        text=text,
        original_tokens=original_tokens,
        skeleton_tokens=estimate_tokens(text),
        method=method,
    )


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python codeskeleton.py <file>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    result = skeletonize(path.read_text(encoding="utf-8"), filename=path.name)
    print(result.text)
    print(
        f"\n--- {result.method}: {result.original_tokens} -> {result.skeleton_tokens} tokens "
        f"({result.saved_pct:.0f}% saved) ---",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
