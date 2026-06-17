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

Phase 2 (ADR-0018): each SymbolSpan also carries `search_text` — a deterministic,
deduped, sorted concatenation of words derived from the symbol's identifier, docstring,
signature, decorators, called names, and imported names within the span. This is the
retrieval surface for concept-word queries that don't appear verbatim in the raw body.
Python-first; non-Python symbols have search_text=None.

Python-first by design (ADR-0018): non-Python suffixes return no spans, so those symbols
stay graph-only (body_text=None) until per-language recovery lands. No fabricated spans.
"""

import ast
import re
from dataclasses import dataclass

from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})

# ---------------------------------------------------------------------------
# Identifier splitting (snake_case + camelCase -> words)
# ---------------------------------------------------------------------------

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _split_identifier(name: str) -> list[str]:
    """Split a snake_case/camelCase identifier into lowercase words.

    Examples:
        validate_token      -> ["validate", "token"]
        AuthMiddleware      -> ["auth", "middleware"]
        HTTPSHandler        -> ["https", "handler"]
        get_user_by_id      -> ["get", "user", "by", "id"]
    """
    # First split camelCase/PascalCase boundaries, then split on underscores/non-alnum.
    spaced = _CAMEL_BOUNDARY.sub(" ", name)
    parts = _NON_ALNUM.split(spaced.lower())
    return [p for p in parts if len(p) >= 2]  # drop single-char remnants ("a", "i")


def _collect_called_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> list[str]:
    """Collect the top-level name of every ast.Call found within the node's body.

    Only the leading attribute/name is collected (e.g. ``foo.bar()`` -> ``bar``,
    ``helper()`` -> ``helper``), not the fully qualified chain, to stay comparable
    with split-identifier words. Pure AST — no LLM, no I/O.
    """
    names: list[str] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.append(func.id)
        elif isinstance(func, ast.Attribute):
            names.append(func.attr)
    return names


def _collect_import_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> list[str]:
    """Collect names bound by import statements within the node (rare but valid).

    e.g. a function that does ``import os`` or ``from pathlib import Path`` inside
    its body contributes those names to the retrieval surface so a query for
    "pathlib" or "Path" finds the function.
    """
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                # alias.asname if present, else the top-level module name
                bound = alias.asname or alias.name.split(".")[0]
                names.append(bound)
        elif isinstance(child, ast.ImportFrom):
            for alias in child.names:
                bound = alias.asname or alias.name
                names.append(bound)
    return names


def build_search_text(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> str:
    """Compose the deterministic search_text for one Python symbol node.

    Collects (deterministic, no LLM):
    - Split words from the symbol's own identifier (snake_case / camelCase).
    - Each arg/kwarg/vararg name from the signature (split identifiers).
    - Each decorator's top-level name (split identifiers).
    - The full docstring text (verbatim; already contains human-readable words).
    - Top-level names of all ast.Call sites within the node (split identifiers).
    - Names bound by any import statements within the node (split identifiers).

    Result: all unique words, sorted (deterministic: same source => same output),
    joined by a single space.
    """
    words: set[str] = set()

    # 1. Symbol name itself (split)
    words.update(_split_identifier(node.name))

    # 2. Signature: arg names (split); skip 'self'/'cls' — not useful for search
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        args = node.args
        for arg in (*args.args, *args.posonlyargs, *args.kwonlyargs):
            if arg.arg not in ("self", "cls"):
                words.update(_split_identifier(arg.arg))
        if args.vararg and args.vararg.arg not in ("self", "cls"):
            words.update(_split_identifier(args.vararg.arg))
        if args.kwarg and args.kwarg.arg not in ("self", "cls"):
            words.update(_split_identifier(args.kwarg.arg))

    # 3. Decorator names (split)
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name):
            words.update(_split_identifier(decorator.id))
        elif isinstance(decorator, ast.Attribute):
            words.update(_split_identifier(decorator.attr))
        elif isinstance(decorator, ast.Call):
            func = decorator.func
            if isinstance(func, ast.Name):
                words.update(_split_identifier(func.id))
            elif isinstance(func, ast.Attribute):
                words.update(_split_identifier(func.attr))

    # 4. Docstring (verbatim words — already human-readable)
    docstring = ast.get_docstring(node) or ""
    if docstring:
        for word in re.split(r"\W+", docstring.lower()):
            if len(word) >= 2:
                words.add(word)

    # 5. Called names within the node body (split)
    for name in _collect_called_names(node):
        words.update(_split_identifier(name))

    # 6. Import names within the node body (split)
    for name in _collect_import_names(node):
        words.update(_split_identifier(name))

    # Drop empty strings that can slip through splitting
    words.discard("")

    return " ".join(sorted(words))


@dataclass(frozen=True)
class SymbolSpan:
    """One symbol's exact 1-based inclusive line span and recovered body text.

    `def_line` is the symbol's def/class line (Graphify's join key). `span_start`
    is decorator-inclusive (<= def_line) so the body text captures decorators; the
    leading docstring is already inside `span_start..span_end`.

    `search_text` (ADR-0018 Phase 2) is the deterministic retrieval surface: split
    identifier words + docstring words + signature + decorators + called names +
    imported names. None for non-Python symbols (populated only by the AST pass).
    """

    name: str
    def_line: int
    span_start: int
    span_end: int
    body_text: str
    search_text: str | None = None


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
        search_text = build_search_text(node)
        by_def_line.setdefault(node.lineno, []).append(
            SymbolSpan(
                name=node.name,
                def_line=node.lineno,
                span_start=span_start,
                span_end=span_end,
                body_text=body_text,
                search_text=search_text or None,
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


__all__ = [
    "SymbolSpan",
    "build_search_text",
    "recover_python_spans",
    "recover_spans",
]
