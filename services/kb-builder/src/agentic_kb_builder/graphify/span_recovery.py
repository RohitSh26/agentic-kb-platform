"""Deterministic exact-span recovery for code symbols (ADR-0018 phase 1).

The external Graphify extractor emits only a symbol's START line (kind/label/imports),
never an end position, so its `code_symbol` artifacts have no citable body. This module
recovers each symbol's EXACT source span with a pure Python `ast` pass over the file
text — NO LLM — so `code_symbol.body_text` becomes the real source an agent cites.

The span runs from the symbol's leading decorators through its last line (inclusive,
1-based) and therefore captures decorators + the leading docstring already inside the
body. Spans are keyed by the symbol's `def`/`class` line, which is exactly what Graphify
reports as `source_location` (verified: Graphify uses `ast.lineno`, the def/class line,
NOT the decorator line). The bare symbol name is carried alongside so a caller can
disambiguate the rare case of several symbols starting on one physical line.

Python-first by design (ADR-0018): non-Python suffixes return no spans, so those symbols
stay graph-only (body_text=None) until per-language recovery lands. No fabricated spans.
"""

import ast
from dataclasses import dataclass

from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})


@dataclass(frozen=True)
class SymbolSpan:
    """One symbol's exact 1-based inclusive line span and recovered body text.

    `def_line` is the symbol's def/class line (Graphify's join key). `span_start`
    is decorator-inclusive (<= def_line) so the body text captures decorators; the
    leading docstring is already inside `span_start..span_end`.
    """

    name: str
    def_line: int
    span_start: int
    span_end: int
    body_text: str


def _node_span(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> tuple[int, int]:
    """(decorator-inclusive start, end) 1-based inclusive lines for one definition."""
    start = node.lineno
    for decorator in node.decorator_list:
        start = min(start, decorator.lineno)
    # end_lineno is always populated on 3.12 def/class nodes; guard defensively so a
    # missing end never fabricates a span — fall back to the def line (single line).
    end = node.end_lineno if node.end_lineno is not None else node.lineno
    return start, end


def recover_python_spans(*, file_text: str, path: str) -> dict[int, list[SymbolSpan]]:
    """Map each definition's def/class line -> the SymbolSpan(s) starting on it.

    Pure and deterministic: same text ⇒ same spans. A syntax error (a file Graphify's
    tolerant parser may still have produced nodes for) yields no spans rather than
    raising — the symbols stay graph-only and the build is not aborted. The list value
    handles the rare several-defs-on-one-line collision (disambiguated by name).
    """
    try:
        tree = ast.parse(file_text)
    except SyntaxError as error:
        logger.warning(
            "event=span_recovery_parse_failed path=%s error=%s",
            path,
            f"{type(error).__name__}: {error}",
        )
        return {}

    lines = file_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    by_def_line: dict[int, list[SymbolSpan]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        span_start, span_end = _node_span(node)
        if span_end > len(lines):
            # The parsed span runs past the captured text — the file we hashed does
            # not match what we parsed; skip rather than fabricate a truncated body.
            logger.warning(
                "event=span_recovery_span_past_eof path=%s name=%s span=%d-%d lines=%d",
                path,
                node.name,
                span_start,
                span_end,
                len(lines),
            )
            continue
        body_text = "\n".join(lines[span_start - 1 : span_end])
        by_def_line.setdefault(node.lineno, []).append(
            SymbolSpan(
                name=node.name,
                def_line=node.lineno,
                span_start=span_start,
                span_end=span_end,
                body_text=body_text,
            )
        )
    logger.info(
        "event=span_recovery_completed path=%s symbols=%d",
        path,
        sum(len(spans) for spans in by_def_line.values()),
    )
    return by_def_line


def recover_spans(*, file_text: str, suffix: str, path: str) -> dict[int, list[SymbolSpan]]:
    """Dispatch span recovery by file suffix. Python-first; other languages return
    no spans (graph-only, body_text=None) until per-language recovery lands."""
    if suffix.lower() in _PYTHON_SUFFIXES:
        return recover_python_spans(file_text=file_text, path=path)
    logger.info("event=span_recovery_unsupported_language path=%s suffix=%s", path, suffix)
    return {}


def extract_import_modules(*, file_text: str, suffix: str, path: str) -> tuple[str, ...]:
    """Return the ABSOLUTE imported module dotted-names for a Python source file.

    Rules:
    - ``import a.b.c`` -> ``"a.b.c"``
    - ``from a.b import c`` -> ``"a.b"`` (the containing package, not the attribute)
    - Relative imports (level > 0, e.g. ``from . import x``) are SKIPPED — V1
      limitation; resolving relative imports requires the full package layout.
      Follow-up: emit relative edges once the build knows the full dotted package path.
    - Non-Python suffixes -> empty (Python-first, same convention as recover_spans).
    - Syntax errors -> empty (never raise; the build continues).

    The result is deterministic: same text + suffix -> same tuple.
    """
    if suffix.lower() not in _PYTHON_SUFFIXES:
        return ()
    try:
        tree = ast.parse(file_text)
    except SyntaxError as error:
        logger.warning(
            "event=import_extract_parse_failed path=%s error=%s",
            path,
            f"{type(error).__name__}: {error}",
        )
        return ()
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import — skip (V1 limitation, requires package-layout knowledge).
                continue
            if node.module:
                modules.append(node.module)
    logger.info(
        "event=import_extract_completed path=%s modules=%d",
        path,
        len(modules),
    )
    return tuple(modules)


__all__ = [
    "SymbolSpan",
    "extract_import_modules",
    "recover_python_spans",
    "recover_spans",
]
