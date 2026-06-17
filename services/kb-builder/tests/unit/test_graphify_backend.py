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


def test_symbol_without_spans_stays_pointer_style() -> None:
    # No `spans` argument (a non-Python language, or a caller that skips recovery):
    # the symbol keeps its start-line pointer with no fabricated end or body
    # (ADR-0018 leaves unsupported-language symbols graph-only).
    by_key = {a.key: a for a in _result().artifacts}
    assert by_key["sym:pkg/service.py::top"].span_start == 7
    assert by_key["sym:pkg/service.py::top"].span_end is None
    assert by_key["sym:pkg/service.py::top"].body_text is None


def test_spans_attach_exact_body_text_to_matched_symbols() -> None:
    # ADR-0018: a recovered span map (keyed by def-line) attaches the EXACT source
    # body_text to the symbol whose Graphify start line matches. `top` starts at L7.
    from agentic_kb_builder.graphify.span_recovery import SymbolSpan

    spans = {
        7: [
            SymbolSpan(
                name="top", def_line=7, span_start=7, span_end=9, body_text="def top():\n    ..."
            )
        ]
    }
    by_key = {
        a.key: a
        for a in map_extraction(GRAPH, spans_by_file={"pkg/service.py": spans}).artifacts
    }
    top = by_key["sym:pkg/service.py::top"]
    assert top.span_start == 7
    assert top.span_end == 9
    assert top.body_text == "def top():\n    ..."
    # A symbol with no matching span stays pointer-style (body_text None).
    assert by_key["sym:pkg/util.py::helper"].body_text is None


def test_span_collision_disambiguated_by_name() -> None:
    # Two defs on one physical line (def_line 7) are resolved by the bare label name.
    from agentic_kb_builder.graphify.span_recovery import SymbolSpan

    spans = {
        7: [
            SymbolSpan(name="other", def_line=7, span_start=7, span_end=7, body_text="WRONG"),
            SymbolSpan(name="top", def_line=7, span_start=7, span_end=9, body_text="RIGHT"),
        ]
    }
    by_key = {
        a.key: a
        for a in map_extraction(GRAPH, spans_by_file={"pkg/service.py": spans}).artifacts
    }
    # The fixture's L7 symbol carries label "top()" -> matches the "top" span.
    assert by_key["sym:pkg/service.py::top"].body_text == "RIGHT"


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
    # Graphify's own contains/method relations never become edges; the only edge types
    # we emit are imports, calls (from the graph) and our deterministic defined_in (ADR-0020).
    assert types <= {"imports", "calls", "defined_in"}


def test_every_symbol_links_to_its_file_via_defined_in() -> None:
    result = _result()
    files = {a.key for a in result.artifacts if a.artifact_type == "code_file"}
    symbols = [a for a in result.artifacts if a.artifact_type == "code_symbol"]
    defined_in = {(e.from_key, e.to_key) for e in result.edges if e.edge_type == "defined_in"}
    assert symbols and files
    for sym in symbols:
        # exactly one defined_in edge, pointing at a real code_file artifact (never dangling).
        targets = [t for (f, t) in defined_in if f == sym.key]
        assert len(targets) == 1, sym.key
        assert targets[0] in files


def test_mapping_is_deterministic() -> None:
    assert map_extraction(GRAPH) == _result()
