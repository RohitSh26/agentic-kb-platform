"""file_graph_to_edges dedupes identical edge drafts within one file.

KB-1 (#22): graphify emits one edge per call site / import statement, so a symbol
that calls or imports another more than once produced redundant identical
(from, to, edge_type) rows. The drafter now collapses them to one logical fact.
"""

from agentic_kb_builder.domain.graph_artifacts import (
    FileGraph,
    ParsedCall,
    ParsedImport,
    ParsedTest,
)
from agentic_kb_builder.graphify.to_edges import file_graph_to_edges


def test_repeated_calls_and_imports_collapse_to_one_edge_each() -> None:
    graph = FileGraph(
        path="app/service.py",
        calls=(
            ParsedCall(from_symbol="handler", to_symbol="validate"),
            ParsedCall(from_symbol="handler", to_symbol="validate"),  # second call site
            ParsedCall(from_symbol="handler", to_symbol="persist"),  # distinct target
        ),
        imports=(
            ParsedImport(target_path="app/db.py"),
            ParsedImport(target_path="app/db.py"),  # imported again
        ),
    )

    edges = file_graph_to_edges(graph)

    keys = [(e.from_key, e.to_key, e.edge_type) for e in edges]
    assert len(keys) == len(set(keys)), f"duplicate drafts not collapsed: {keys}"
    calls = [e for e in edges if e.edge_type == "calls"]
    imports = [e for e in edges if e.edge_type == "imports"]
    assert len(calls) == 2  # handler->validate and handler->persist, deduped
    assert len(imports) == 1  # service.py->db.py, deduped


def test_distinct_edges_preserved_in_order_and_test_targets_deduped() -> None:
    graph = FileGraph(
        path="app/service.py",
        calls=(
            ParsedCall(from_symbol="a", to_symbol="b"),
            ParsedCall(from_symbol="a", to_symbol="c"),
        ),
        tests=(ParsedTest(name="test_a", targets=("a", "a"), span_start=1, span_end=2),),
    )

    edges = file_graph_to_edges(graph)

    # both distinct calls survive in order; the repeated test target collapses to one
    assert [e.edge_type for e in edges] == ["calls", "calls", "tests"]
