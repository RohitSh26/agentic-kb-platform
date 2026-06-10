"""Graphify adapter: parsed code graphs -> canonical code artifacts/edges (architecture §5.2)."""

from kb_builder.graphify_adapter.keys import (
    ParsedKey,
    endpoint_key,
    file_key,
    parse_key,
    symbol_key,
    test_key,
)
from kb_builder.graphify_adapter.parse import parse_file_graph
from kb_builder.graphify_adapter.to_artifacts import file_graph_to_artifacts
from kb_builder.graphify_adapter.to_edges import (
    CALLS_CONFIDENCE,
    EXPOSED_AS_CONFIDENCE,
    IMPORTS_CONFIDENCE,
    TESTS_CONFIDENCE,
    file_graph_to_edges,
)
from kb_builder.graphify_adapter.write import (
    BUILD_TIME_FRESHNESS,
    CODE_AUTHORITY,
    EDGE_SOURCE,
    write_code_artifacts,
    write_code_edges,
)

__all__ = [
    "BUILD_TIME_FRESHNESS",
    "CALLS_CONFIDENCE",
    "CODE_AUTHORITY",
    "EDGE_SOURCE",
    "EXPOSED_AS_CONFIDENCE",
    "IMPORTS_CONFIDENCE",
    "TESTS_CONFIDENCE",
    "ParsedKey",
    "endpoint_key",
    "file_graph_to_artifacts",
    "file_graph_to_edges",
    "file_key",
    "parse_file_graph",
    "parse_key",
    "symbol_key",
    "test_key",
    "write_code_artifacts",
    "write_code_edges",
]
