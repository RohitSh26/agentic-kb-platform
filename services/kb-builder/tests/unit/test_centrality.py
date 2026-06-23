"""Deterministic PageRank for the graph-centrality ranking prior (ADR-0028 / PR-36).

Pure-function tests (no DB): determinism (incl. shuffled input + dangling nodes), the
referenced-node-outranks-leaf property, edgeless ⇒ all-zero, and normalization range.
"""

import random

from agentic_kb_builder.application.centrality import pagerank


def test_referenced_node_outranks_a_leaf() -> None:
    # b and c both point at a; a is the hub. d is an unreferenced leaf.
    nodes = {"a", "b", "c", "d"}
    edges = [("b", "a"), ("c", "a"), ("d", "a")]
    scores = pagerank(edges, nodes)
    assert scores["a"] == 1.0  # the most-referenced node normalizes to the peak
    assert scores["a"] > scores["b"]
    assert scores["a"] > scores["d"]


def test_normalized_into_unit_range() -> None:
    nodes = {"a", "b", "c"}
    edges = [("a", "b"), ("b", "c"), ("c", "b")]
    scores = pagerank(edges, nodes)
    assert all(0.0 <= s <= 1.0 for s in scores.values())
    assert max(scores.values()) == 1.0


def test_edgeless_graph_is_all_zero_no_crash() -> None:
    assert pagerank([], {"a", "b", "c"}) == {"a": 0.0, "b": 0.0, "c": 0.0}
    assert pagerank([], set()) == {}


def test_deterministic_across_runs_and_shuffled_input() -> None:
    nodes = {f"n{i}" for i in range(12)}
    edges = [(f"n{i}", f"n{(i * 7 + 3) % 12}") for i in range(40)]
    a = pagerank(edges, nodes)
    shuffled = edges[:]
    random.Random(1).shuffle(shuffled)
    b = pagerank(shuffled, set(nodes))
    # bit-identical regardless of input row order (sorted node + adjacency order)
    assert a == b


def test_dangling_nodes_are_handled_deterministically() -> None:
    # n3 and n4 have no out-edges (dangling); their mass must redistribute without
    # breaking determinism or the unit-range normalization.
    nodes = {"n1", "n2", "n3", "n4"}
    edges = [("n1", "n2"), ("n2", "n3"), ("n1", "n4")]
    first = pagerank(edges, nodes)
    second = pagerank(list(reversed(edges)), nodes)
    assert first == second
    assert all(0.0 <= s <= 1.0 for s in first.values())


def test_edges_to_unknown_nodes_are_ignored() -> None:
    # an edge endpoint outside the node set must not crash or invent a node.
    scores = pagerank([("a", "ghost"), ("b", "a")], {"a", "b"})
    assert set(scores) == {"a", "b"}
    assert scores["a"] >= scores["b"]
