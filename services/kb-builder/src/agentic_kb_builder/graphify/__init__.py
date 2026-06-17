"""Graphify adapter: whole-tree Graphify extraction -> canonical code artifacts/edges
(architecture §5.2). Code structure is delegated to the Graphify library (ADR-0012);
this package only maps its output into our versioned, ACL'd Postgres artifacts/edges."""

from agentic_kb_builder.graphify.graphify_backend import graphify_tree, map_extraction
from agentic_kb_builder.graphify.keys import (
    ParsedKey,
    endpoint_key,
    file_key,
    parse_key,
    symbol_key,
    test_key,
)
from agentic_kb_builder.graphify.to_edges import (
    CALLS_CONFIDENCE,
    EXPOSED_AS_CONFIDENCE,
    IMPORTS_CONFIDENCE,
    TESTS_CONFIDENCE,
)
from agentic_kb_builder.graphify.write import (
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
    "file_key",
    "graphify_tree",
    "map_extraction",
    "parse_key",
    "symbol_key",
    "test_key",
    "write_code_artifacts",
    "write_code_edges",
]
