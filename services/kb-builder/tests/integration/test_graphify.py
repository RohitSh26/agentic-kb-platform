"""Unit tests for the graphify adapter (no DB): symbolic keys + whole-tree extraction.

The per-file FileGraph mapping (file_graph_to_* / parse_file_graph) was deleted when the
build moved to whole-tree Graphify (ADR-0012); these tests now drive the REAL Graphify
library over real Python source and assert on its deterministic, offline output.
"""

import pytest

from agentic_kb_builder.graphify import (
    ParsedKey,
    graphify_tree,
    parse_key,
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


def test_graphify_tree_empty_input_is_empty() -> None:
    result = graphify_tree([])
    assert result.artifacts == ()
    assert result.edges == ()


def test_graphify_tree_single_function_file() -> None:
    """One file with one function: a code_file (pointer-only) + a code_symbol with the
    exact recovered span/body_text, plus a `defined_in` symbol->file edge."""
    result = graphify_tree([("a.py", "def get_user():\n    return 1\n")])
    by_key = {a.key: a for a in result.artifacts}
    assert set(by_key) == {"file:a.py", "sym:a.py::get_user"}

    code_file = by_key["file:a.py"]
    assert code_file.artifact_type == "code_file"
    assert code_file.body_text is None and code_file.span_start is None

    symbol = by_key["sym:a.py::get_user"]
    assert symbol.artifact_type == "code_symbol"
    assert (symbol.span_start, symbol.span_end) == (1, 2)
    assert symbol.body_text == "def get_user():\n    return 1"

    edges = {(e.edge_type, e.from_key, e.to_key): e for e in result.edges}
    defined_in = edges[("defined_in", "sym:a.py::get_user", "file:a.py")]
    assert defined_in.confidence == 1.0


def test_graphify_tree_code_file_carries_the_skeleton_as_search_text() -> None:
    """ADR-0033 (PR-42 grounded decision): the Python file's deterministic skeleton
    becomes the code_file artifact's search_text — display/search material only.
    body_text stays None (pointer-only) and the symbol's raw span + word bag are
    untouched, so the governed L2/verify paths keep serving raw source."""
    source = 'def get_user():\n    """Load one user."""\n    row = db()\n    return row\n'
    result = graphify_tree([("a.py", source)])
    by_key = {a.key: a for a in result.artifacts}

    code_file = by_key["file:a.py"]
    assert code_file.body_text is None  # still pointer-only: never a stored raw document
    assert code_file.search_text is not None
    assert "def get_user():" in code_file.search_text
    assert '"""Load one user."""' in code_file.search_text
    assert "row = db()" not in code_file.search_text  # the body is elided
    assert "lines elided" in code_file.search_text

    symbol = by_key["sym:a.py::get_user"]
    assert symbol.body_text is not None
    assert "row = db()" in symbol.body_text  # raw citable span, untouched
    assert symbol.search_text is not None
    assert "def " not in symbol.search_text  # still the ADR-0018 word bag, not a skeleton
    assert "load" in symbol.search_text.split()


def test_graphify_tree_skeleton_is_deterministic_across_runs() -> None:
    source = 'def f(x: int) -> int:\n    """Double."""\n    y = x * 2\n    return y\n'
    first = graphify_tree([("d.py", source)])
    second = graphify_tree([("d.py", source)])
    skeleton = {a.key: a.search_text for a in first.artifacts}
    assert skeleton == {a.key: a.search_text for a in second.artifacts}


def test_graphify_tree_assignment_only_file_has_no_symbol_or_edge() -> None:
    result = graphify_tree([("x.py", "x = 1\n")])
    assert {a.key for a in result.artifacts} == {"file:x.py"}
    assert result.edges == ()


def test_graphify_tree_resolves_cross_file_imports_and_calls() -> None:
    """Two files in one tree: Graphify resolves the import (file->file) and the call
    (symbol->symbol) across files — the capability whole-tree extraction provides."""
    result = graphify_tree(
        [
            ("a2.py", "from b import thing\n\ndef run():\n    return thing()\n"),
            ("b.py", "def thing():\n    return 2\n"),
        ]
    )
    keys = {a.key for a in result.artifacts}
    assert {"file:a2.py", "sym:a2.py::run", "file:b.py", "sym:b.py::thing"} <= keys

    edge_keys = {(e.edge_type, e.from_key, e.to_key) for e in result.edges}
    assert ("defined_in", "sym:a2.py::run", "file:a2.py") in edge_keys
    assert ("defined_in", "sym:b.py::thing", "file:b.py") in edge_keys
    assert ("imports", "file:a2.py", "file:b.py") in edge_keys
    assert ("calls", "sym:a2.py::run", "sym:b.py::thing") in edge_keys


def test_graphify_tree_drops_out_of_tree_stdlib_imports() -> None:
    """A stdlib import (`os`) is out-of-tree and produces NO file->file edge — the
    adapter never fabricates a node for a reference it did not extract."""
    result = graphify_tree([("c.py", "import os\n\ndef f():\n    return os.getcwd()\n")])
    import_edges = [e for e in result.edges if e.edge_type == "imports"]
    assert import_edges == []
    # the defined_in edge still exists for the local function.
    assert ("defined_in", "sym:c.py::f", "file:c.py") in {
        (e.edge_type, e.from_key, e.to_key) for e in result.edges
    }
