"""Graphify-output normalization (ADR-0012), hermetic — no Graphify run, no DB, no LLM.

Feeds a captured Graphify extraction (the 2-file fixture from the spike) through
`map_extraction` and asserts the trust-preserving rules: vocabulary maps to our
ontology, structural relations are dropped, external imports are dropped, and a
name-collision call site is dropped wholesale rather than stored as a fabricated edge.
"""

from agentic_kb_builder.graphify.graphify_backend import map_extraction

# Captured from `graphify update src --no-cluster` on the spike fixture (7 nodes, 12 links).
# pkg/service.py imports pkg/util.py; `helper()` at L8 collides between util.helper and
# the Service.helper method.
GRAPH = {
    "nodes": [
        {
            "id": "pkg_service",
            "label": "service.py",
            "source_file": "pkg/service.py",
            "source_location": "L1",
        },
        {
            "id": "pkg_service_top",
            "label": "top()",
            "source_file": "pkg/service.py",
            "source_location": "L7",
        },
        {
            "id": "pkg_service_service",
            "label": "Service",
            "source_file": "pkg/service.py",
            "source_location": "L11",
        },
        {
            "id": "pkg_service_service_handle",
            "label": ".handle()",
            "source_file": "pkg/service.py",
            "source_location": "L12",
        },
        {
            "id": "pkg_service_service_helper",
            "label": ".helper()",
            "source_file": "pkg/service.py",
            "source_location": "L15",
        },
        {
            "id": "pkg_util",
            "label": "util.py",
            "source_file": "pkg/util.py",
            "source_location": "L1",
        },
        {
            "id": "pkg_util_helper",
            "label": "helper()",
            "source_file": "pkg/util.py",
            "source_location": "L1",
        },
    ],
    "links": [
        {
            "source": "pkg_service",
            "target": "os_path",
            "relation": "imports",
            "source_location": "L1",
        },
        {
            "source": "pkg_service",
            "target": "pkg_util",
            "relation": "imports_from",
            "source_location": "L2",
        },
        {
            "source": "pkg_service",
            "target": "pkg_service_top",
            "relation": "contains",
            "source_location": "L7",
        },
        {
            "source": "pkg_service",
            "target": "pkg_service_service",
            "relation": "contains",
            "source_location": "L11",
        },
        {
            "source": "pkg_service_service",
            "target": "pkg_service_service_handle",
            "relation": "method",
            "source_location": "L12",
        },
        {
            "source": "pkg_service_service",
            "target": "pkg_service_service_helper",
            "relation": "method",
            "source_location": "L15",
        },
        {
            "source": "pkg_service_top",
            "target": "pkg_service_service_helper",
            "relation": "calls",
            "source_location": "L8",
        },
        {
            "source": "pkg_service_service_handle",
            "target": "pkg_service_service_helper",
            "relation": "calls",
            "source_location": "L13",
        },
        {
            "source": "pkg_service_service_helper",
            "target": "pkg_service_top",
            "relation": "calls",
            "source_location": "L16",
        },
        {
            "source": "pkg_util",
            "target": "pkg_util_helper",
            "relation": "contains",
            "source_location": "L1",
        },
        {
            "source": "pkg_service",
            "target": "pkg_util_helper",
            "relation": "imports",
            "source_location": "L2",
        },
        {
            "source": "pkg_service_top",
            "target": "pkg_util_helper",
            "relation": "calls",
            "source_location": "L8",
        },
    ],
}


def _result():
    return map_extraction(GRAPH)


def test_files_and_symbols_become_artifacts() -> None:
    result = _result()
    files = {a.key for a in result.artifacts if a.artifact_type == "code_file"}
    symbols = {a.key for a in result.artifacts if a.artifact_type == "code_symbol"}
    assert files == {"file:pkg/service.py", "file:pkg/util.py"}
    assert "sym:pkg/service.py::top" in symbols
    assert "sym:pkg/util.py::helper" in symbols
    assert len(result.artifacts) == 7


def test_symbol_carries_start_line_pointer_span() -> None:
    by_key = {a.key: a for a in _result().artifacts}
    assert by_key["sym:pkg/service.py::top"].span_start == 7
    # Pointer-style until span recovery (ADR-0012): no fabricated end or snippet.
    assert by_key["sym:pkg/service.py::top"].span_end is None
    assert by_key["sym:pkg/service.py::top"].body_text is None


def test_imports_map_to_file_edges_and_drop_externals() -> None:
    edges = {(e.from_key, e.to_key, e.edge_type) for e in _result().edges}
    assert ("file:pkg/service.py", "file:pkg/util.py", "imports") in edges
    # os.path is external (no node) -> no import edge to it.
    assert not any(e.edge_type == "imports" and "os" in e.to_key for e in _result().edges)


def test_ambiguous_call_site_is_dropped_not_stored() -> None:
    calls = {(e.from_key, e.to_key) for e in _result().edges if e.edge_type == "calls"}
    # L8 `helper()` collides (util.helper vs Service.helper) -> whole site dropped.
    assert not any(frm == "sym:pkg/service.py::top" for frm, _ in calls)
    # Unambiguous intra-file calls survive.
    assert ("sym:pkg/service.py::service_handle", "sym:pkg/service.py::service_helper") in calls
    assert ("sym:pkg/service.py::service_helper", "sym:pkg/service.py::top") in calls


def test_structural_relations_produce_no_edges() -> None:
    types = {e.edge_type for e in _result().edges}
    assert types <= {"imports", "calls"}  # contains/method never become edges


def test_mapping_is_deterministic() -> None:
    assert map_extraction(GRAPH) == _result()
