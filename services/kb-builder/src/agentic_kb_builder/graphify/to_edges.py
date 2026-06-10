"""FileGraph -> code edge drafts by symbolic key (resolved to uuids at write time)."""

from agentic_kb_builder.domain import CodeEdgeDraft, FileGraph
from agentic_kb_builder.graphify.keys import endpoint_key, file_key, symbol_key, test_key
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Imports and route declarations are read directly off the AST; call and
# test-target extraction can be confused by dynamic dispatch, hence < 1.0.
IMPORTS_CONFIDENCE = 1.0
EXPOSED_AS_CONFIDENCE = 1.0
CALLS_CONFIDENCE = 0.9
TESTS_CONFIDENCE = 0.9


def file_graph_to_edges(graph: FileGraph) -> tuple[CodeEdgeDraft, ...]:
    edges: list[CodeEdgeDraft] = []
    source_file = file_key(graph.path)
    for imported in graph.imports:
        edges.append(
            CodeEdgeDraft(
                from_key=source_file,
                to_key=file_key(imported.target_path),
                edge_type="imports",
                confidence=IMPORTS_CONFIDENCE,
            )
        )
    for call in graph.calls:
        edges.append(
            CodeEdgeDraft(
                from_key=symbol_key(graph.path, call.from_symbol),
                to_key=_symbol_ref_key(graph.path, call.to_symbol),
                edge_type="calls",
                confidence=CALLS_CONFIDENCE,
            )
        )
    for test in graph.tests:
        for target in test.targets:
            edges.append(
                CodeEdgeDraft(
                    from_key=test_key(graph.path, test.name),
                    to_key=_symbol_ref_key(graph.path, target),
                    edge_type="tests",
                    confidence=TESTS_CONFIDENCE,
                )
            )
    for endpoint in graph.endpoints:
        edges.append(
            CodeEdgeDraft(
                from_key=symbol_key(graph.path, endpoint.symbol),
                to_key=endpoint_key(graph.path, endpoint.http_method, endpoint.route),
                edge_type="exposed_as",
                confidence=EXPOSED_AS_CONFIDENCE,
            )
        )
    logger.info("event=graphify_edges_drafted path=%s count=%d", graph.path, len(edges))
    return tuple(edges)


def _symbol_ref_key(default_path: str, ref: str) -> str:
    """Symbol refs are a same-file name or a cross-file 'path::name'."""
    if "::" in ref:
        path, _, name = ref.rpartition("::")
        return symbol_key(path, name)
    return symbol_key(default_path, ref)
