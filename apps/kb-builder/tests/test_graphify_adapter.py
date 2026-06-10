"""Unit tests for the graphify adapter (no DB): parse, keys, artifacts, edges."""

import pytest
from pydantic import ValidationError

from contracts.artifact_schemas import (
    FileGraph,
    ParsedCall,
    ParsedEndpoint,
    ParsedImport,
    ParsedSymbol,
    ParsedTest,
)
from kb_builder.graphify_adapter import (
    CALLS_CONFIDENCE,
    EXPOSED_AS_CONFIDENCE,
    IMPORTS_CONFIDENCE,
    TESTS_CONFIDENCE,
    ParsedKey,
    file_graph_to_artifacts,
    file_graph_to_edges,
    parse_file_graph,
    parse_key,
)

GRAPH = FileGraph(
    path="api/users.py",
    symbols=(ParsedSymbol(name="get_user", kind="function", span_start=1, span_end=2),),
    endpoints=(ParsedEndpoint(http_method="GET", route="/users/{id}", symbol="get_user"),),
    tests=(ParsedTest(name="test_get_user", span_start=4, span_end=5, targets=("get_user",)),),
    imports=(ParsedImport(target_path="lib/util.py"),),
    calls=(
        ParsedCall(from_symbol="get_user", to_symbol="lib/util.py::helper"),
        ParsedCall(from_symbol="get_user", to_symbol="validate"),
    ),
)
FILE_TEXT = "line1\nline2\nline3\nline4\nline5\n"


def test_parse_file_graph_validates_raw_payload() -> None:
    raw = {
        "path": "a.py",
        "symbols": [{"name": "f", "kind": "function", "span_start": 1, "span_end": 2}],
    }
    graph = parse_file_graph(raw)
    assert graph.path == "a.py"
    assert graph.symbols[0].name == "f"

    with pytest.raises(ValidationError):  # span_end < span_start
        parse_file_graph(
            {
                "path": "a.py",
                "symbols": [{"name": "f", "kind": "function", "span_start": 3, "span_end": 1}],
            }
        )


def test_parse_key_round_trips_every_scheme() -> None:
    assert parse_key("file:api/users.py") == ParsedKey("code_file", "api/users.py", None)
    assert parse_key("sym:api/users.py::get_user") == ParsedKey(
        "code_symbol", "api/users.py", "get_user"
    )
    assert parse_key("test:api/users.py::test_get_user") == ParsedKey(
        "test", "api/users.py", "test_get_user"
    )
    assert parse_key("endpoint:api/users.py::GET /users/{id}") == ParsedKey(
        "endpoint", "api/users.py", "GET /users/{id}"
    )
    with pytest.raises(ValueError, match="malformed"):
        parse_key("sym:no-separator")
    with pytest.raises(ValueError, match="unknown"):
        parse_key("bogus:a.py")


def test_to_artifacts_exact_spans_and_pointer_only_rows() -> None:
    drafts = {draft.key: draft for draft in file_graph_to_artifacts(GRAPH, file_text=FILE_TEXT)}
    assert set(drafts) == {
        "file:api/users.py",
        "sym:api/users.py::get_user",
        "test:api/users.py::test_get_user",
        "endpoint:api/users.py::GET /users/{id}",
    }
    file_draft = drafts["file:api/users.py"]
    assert file_draft.body_text is None and file_draft.span_start is None
    symbol = drafts["sym:api/users.py::get_user"]
    assert symbol.body_text == "line1\nline2"
    assert (symbol.span_start, symbol.span_end) == (1, 2)
    test_draft = drafts["test:api/users.py::test_get_user"]
    assert test_draft.body_text == "line4\nline5"
    endpoint = drafts["endpoint:api/users.py::GET /users/{id}"]
    assert endpoint.title == "GET /users/{id}" and endpoint.body_text is None


def test_to_artifacts_rejects_span_past_end_of_file() -> None:
    graph = FileGraph(
        path="a.py",
        symbols=(ParsedSymbol(name="f", kind="function", span_start=1, span_end=9),),
    )
    with pytest.raises(ValueError, match="exceeds"):
        file_graph_to_artifacts(graph, file_text="only\ntwo\n")


def test_to_edges_symbolic_keys_types_and_confidence() -> None:
    edges = {(edge.edge_type, edge.to_key): edge for edge in file_graph_to_edges(GRAPH)}
    assert len(edges) == 5

    imports = edges[("imports", "file:lib/util.py")]
    assert imports.from_key == "file:api/users.py"
    assert imports.confidence == IMPORTS_CONFIDENCE

    cross_file_call = edges[("calls", "sym:lib/util.py::helper")]
    assert cross_file_call.from_key == "sym:api/users.py::get_user"
    assert cross_file_call.confidence == CALLS_CONFIDENCE
    same_file_call = edges[("calls", "sym:api/users.py::validate")]
    assert same_file_call.from_key == "sym:api/users.py::get_user"

    tests_edge = edges[("tests", "sym:api/users.py::get_user")]
    assert tests_edge.from_key == "test:api/users.py::test_get_user"
    assert tests_edge.confidence == TESTS_CONFIDENCE

    exposed = edges[("exposed_as", "endpoint:api/users.py::GET /users/{id}")]
    assert exposed.from_key == "sym:api/users.py::get_user"
    assert exposed.confidence == EXPOSED_AS_CONFIDENCE
