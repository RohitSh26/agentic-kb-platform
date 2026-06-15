"""Graphify-backed code extractor (ADR-0012).

Wraps Graphify's (`graphifyy`) public AST API and re-normalizes its output into our
canonical code artifacts/edges. Graphify gives multi-language tree-sitter extraction;
we keep our trust contract by:

- mapping Graphify's vocabulary onto our relation ontology (imports/imports_from ->
  `imports`; calls -> `calls`; structural `contains`/`method` become artifacts, not edges),
- re-deriving trust ourselves and NEVER copying Graphify's `EXTRACTED` label, and
- DROPPING any call site that resolves to more than one target (a syntactic name
  collision, not a resolved semantic call) instead of fabricating a `calls` edge.

Graphify emits only a start line per node. ADR-0018 recovers each Python symbol's
EXACT source span with a deterministic `ast` pass (span_recovery.py) so `code_symbol`
artifacts carry a real, citable `body_text` (start..end incl. decorators/docstring) and
become keyword-searchable with NO LLM. Non-Python symbols stay span-less (body_text=None,
graph-only) until per-language recovery lands.
"""

import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from agentic_kb_builder.domain import (
    CodeArtifactDraft,
    CodeEdgeDraft,
    GraphifyResult,
    NormalizedContent,
)
from agentic_kb_builder.graphify.keys import file_key, symbol_key
from agentic_kb_builder.graphify.span_recovery import SymbolSpan, recover_spans
from agentic_kb_builder.graphify.to_edges import CALLS_CONFIDENCE, IMPORTS_CONFIDENCE
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Graphify relation -> our ontology edge type. Anything not listed (contains, method,
# and any future relation) is structural or out-of-ontology and produces no edge.
_IMPORT_RELATIONS = frozenset({"imports", "imports_from"})
_CALL_RELATIONS = frozenset({"calls"})


def _line(source_location: object) -> int | None:
    """Parse Graphify's `source_location` ("L42") into a 1-based line number."""
    if isinstance(source_location, str) and source_location.startswith("L"):
        try:
            return int(source_location[1:])
        except ValueError:
            return None
    return None


def _label_name(label: object) -> str | None:
    """Bare symbol name from a Graphify node label ("top()", ".handle()", "Service").

    Used only to disambiguate the rare several-defs-on-one-line span collision; the
    primary span join is by start line, which Graphify reports as the def/class line.
    """
    text = str(label).strip()
    text = text.removeprefix(".").removesuffix("()")
    return text or None


def _match_span(
    spans: Mapping[int, list[SymbolSpan]] | None,
    start_line: int | None,
    label: object,
) -> SymbolSpan | None:
    """Resolve a Graphify symbol node to its recovered exact span by start line.

    Graphify's `source_location` is the symbol's def/class line, the same line span
    recovery keys on. A single span on that line is an unambiguous match; several
    (multiple defs sharing one physical line) are disambiguated by bare name. No
    match ⇒ None ⇒ the symbol stays span-less (body_text=None), never fabricated.
    """
    if spans is None or start_line is None:
        return None
    candidates = spans.get(start_line)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    name = _label_name(label)
    for span in candidates:
        if span.name == name:
            return span
    return None


def map_extraction(
    data: Mapping[str, Any],
    *,
    source_file_override: str | None = None,
    file_basename_override: str | None = None,
    spans: Mapping[int, list[SymbolSpan]] | None = None,
) -> GraphifyResult:
    """Normalize one Graphify extraction dict into our artifacts + edges.

    Pure and deterministic — no I/O — so it is hermetically testable against a captured
    `graph.json`. `source_file_override` rewrites every node's source path (used for
    single-file extraction, where Graphify only sees a temp path). `spans` (ADR-0018)
    is the deterministic ast span map keyed by def-line; when a symbol matches, it gets
    its EXACT body_text + span_start/span_end. Without `spans` symbols stay pointer-style.
    """
    nodes = cast("list[Mapping[str, Any]]", list(data.get("nodes", [])))
    raw_edges = data.get("edges")
    if raw_edges is None:
        raw_edges = data.get("links", [])
    edges = cast("list[Mapping[str, Any]]", list(raw_edges))

    def src_file(node: Mapping[str, Any]) -> str:
        return source_file_override or str(node.get("source_file", ""))

    def is_file_node(node: Mapping[str, Any]) -> bool:
        basename = file_basename_override or Path(str(node.get("source_file", ""))).name
        return str(node.get("label", "")) == basename

    # node id -> (our key, source_file); plus the file node id per source_file so we can
    # strip it to make a stable, in-file-unique symbol name.
    file_node_id: dict[str, str] = {}
    for node in nodes:
        if is_file_node(node):
            file_node_id[src_file(node)] = str(node.get("id", ""))

    node_key: dict[str, str] = {}
    node_is_file: dict[str, str] = {}  # id -> source_file (file nodes only)
    artifacts: list[CodeArtifactDraft] = []
    for node in nodes:
        nid = str(node.get("id", ""))
        path = src_file(node)
        if is_file_node(node):
            key = file_key(path)
            node_key[nid] = key
            node_is_file[nid] = path
            artifacts.append(CodeArtifactDraft(key=key, artifact_type="code_file", title=path))
            continue
        prefix = file_node_id.get(path, "")
        name = nid.removeprefix(prefix + "_") if prefix and nid.startswith(prefix + "_") else nid
        key = symbol_key(path, name)
        node_key[nid] = key
        start_line = _line(node.get("source_location"))
        span = _match_span(spans, start_line, node.get("label"))
        if span is not None:
            # ADR-0018: exact deterministic source span (incl. decorators/docstring) is
            # the symbol's citable body_text — no LLM, keyword-searchable. span_start is
            # decorator-inclusive so it may precede Graphify's reported def line.
            artifacts.append(
                CodeArtifactDraft(
                    key=key,
                    artifact_type="code_symbol",
                    title=str(node.get("label", name)),
                    body_text=span.body_text,
                    span_start=span.span_start,
                    span_end=span.span_end,
                )
            )
        else:
            # No recovered span (non-Python language, or an unmatched node): stay
            # pointer-style with the known start line. body_text=None (no fabrication).
            artifacts.append(
                CodeArtifactDraft(
                    key=key,
                    artifact_type="code_symbol",
                    title=str(node.get("label", name)),
                    span_start=start_line,
                )
            )

    edge_drafts: list[CodeEdgeDraft] = []
    seen: set[tuple[str, str, str]] = set()

    def add(from_key: str, to_key: str, edge_type: str, confidence: float) -> None:
        if from_key == to_key:
            return
        key = (from_key, to_key, edge_type)
        if key in seen:
            return
        seen.add(key)
        edge_drafts.append(
            CodeEdgeDraft(
                from_key=from_key,
                to_key=to_key,
                edge_type=cast("Any", edge_type),
                confidence=confidence,
            )
        )

    # Imports -> file->file. Resolve the target to a known node's file; external imports
    # (targets with no node) are dropped — they would not resolve in our graph anyway.
    for edge in edges:
        if str(edge.get("relation", "")) not in _IMPORT_RELATIONS:
            continue
        source_id = str(edge.get("source", ""))
        target_id = str(edge.get("target", ""))
        from_file = node_is_file.get(source_id)
        if from_file is None:
            continue
        target_file = node_is_file.get(target_id) or _node_source_file(nodes, target_id)
        if target_file is None:
            continue
        add(file_key(from_file), file_key(target_file), "imports", IMPORTS_CONFIDENCE)

    # Calls -> symbol->symbol. Group by call site; a site resolving to >1 distinct target
    # is an ambiguous name collision and is dropped wholesale (never stored as EXTRACTED).
    by_site: dict[tuple[str, object], set[str]] = defaultdict(set)
    for edge in edges:
        if str(edge.get("relation", "")) not in _CALL_RELATIONS:
            continue
        source_id = str(edge.get("source", ""))
        target_id = str(edge.get("target", ""))
        if source_id not in node_key or target_id not in node_key:
            continue
        if source_id in node_is_file or target_id in node_is_file:
            continue  # calls are symbol->symbol only
        by_site[(source_id, edge.get("source_location"))].add(target_id)
    for (source_id, _loc), targets in by_site.items():
        if len(targets) != 1:
            logger.info(
                "event=graphify_call_dropped reason=ambiguous_name_collision source=%s targets=%d",
                source_id,
                len(targets),
            )
            continue
        (target_id,) = tuple(targets)
        add(node_key[source_id], node_key[target_id], "calls", CALLS_CONFIDENCE)

    return GraphifyResult(artifacts=tuple(artifacts), edges=tuple(edge_drafts))


def _node_source_file(nodes: Sequence[Mapping[str, Any]], node_id: str) -> str | None:
    for node in nodes:
        if str(node.get("id", "")) == node_id:
            return str(node.get("source_file", "")) or None
    return None


class GraphifyGraphifier:
    """Graphifier (build_runner Protocol) backed by Graphify's public AST extractor.

    Extracts one file at a time to fit the incremental per-file build; cross-file edge
    resolution stays the linker's job (phase 2). Materializes the in-memory content to a
    temp file because Graphify reads from disk and dispatches language by extension.
    """

    async def graphify(self, content: NormalizedContent) -> GraphifyResult:
        from graphify.extract import extract  # declared dependency (ADR-0012)

        path = content.source.path or "unknown"
        suffix = Path(path).suffix or ".py"
        # ADR-0018: recover exact symbol spans deterministically from the SAME text
        # Graphify parsed (no LLM) so code_symbol artifacts get a real, citable body.
        spans = recover_spans(file_text=content.text, suffix=suffix, path=path)
        with tempfile.NamedTemporaryFile("w", suffix=suffix, encoding="utf-8") as handle:
            handle.write(content.text)
            handle.flush()
            tmp = Path(handle.name)
            data = cast("Mapping[str, Any]", extract([tmp], parallel=False))
        return map_extraction(
            data,
            source_file_override=path,
            file_basename_override=tmp.name,
            spans=spans,
        )


__all__ = ["GraphifyGraphifier", "map_extraction"]
