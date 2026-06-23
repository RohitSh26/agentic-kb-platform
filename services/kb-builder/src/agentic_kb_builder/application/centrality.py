"""Deterministic graph centrality (PageRank) over the live knowledge graph.

The broker ranks retrieval by keyword overlap; this adds a *structural* prior — how central a node
is in the code+knowledge graph — which is the relevance signal a plain grep agent cannot compute
(Aider repo-map PageRank, GraphRAG, HippoRAG). Computed at build time, normalized to [0,1], and
stored on `knowledge_artifact.centrality_score` for the broker to fold into its rank key.

Pure graph math: no LLM, no embedding, no model call (so it is NOT cache-gated — a changed edge
anywhere shifts global centrality, so it is recomputed every build that touches edges). Fully
deterministic: sorted node + adjacency order and a fixed damping/iteration/tolerance make the result
bit-identical run to run, independent of input row order.

Read scope: the interval-membership predicate for the active build_seq — NOT only this
build's new edges (an incremental build rewrites one source; the live graph still includes valid
prior-build edges, so ranking over new edges alone would zero almost every score).
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.infrastructure.postgres.models import KnowledgeArtifact, KnowledgeEdge
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_DAMPING = 0.85
_MAX_ITERATIONS = 100
_TOLERANCE = 1e-9


def pagerank(edges: list[tuple[str, str]], nodes: set[str]) -> dict[str, float]:
    """Deterministic PageRank over a directed graph, normalized to [0, 1].

    `edges` are (from, to) node-id pairs; `nodes` is every node to rank (isolated nodes included).
    Score flows TO referenced nodes, so a heavily-depended-on symbol/file ranks high. An edgeless
    graph carries no structural signal, so every node scores 0.0 (a neutral prior).
    """
    node_list = sorted(nodes)
    n = len(node_list)
    if n == 0:
        return {}
    node_set = set(node_list)
    out: dict[str, list[str]] = {u: [] for u in node_list}
    edge_count = 0
    for src, dst in edges:
        if src in node_set and dst in node_set:
            out[src].append(dst)
            edge_count += 1
    if edge_count == 0:
        return {u: 0.0 for u in node_list}  # no structural signal ⇒ neutral
    for u in out:
        out[u].sort()  # deterministic adjacency order

    rank = {u: 1.0 / n for u in node_list}
    teleport = (1.0 - _DAMPING) / n
    for _ in range(_MAX_ITERATIONS):
        # dangling nodes (no out-edges) redistribute their mass uniformly — deterministic.
        dangling = sum(rank[u] for u in node_list if not out[u])
        nxt = {u: teleport + _DAMPING * dangling / n for u in node_list}
        for u in node_list:  # sorted order ⇒ fixed float-summation order
            outs = out[u]
            if outs:
                share = _DAMPING * rank[u] / len(outs)
                for v in outs:
                    nxt[v] += share
        delta = sum(abs(nxt[u] - rank[u]) for u in node_list)
        rank = nxt
        if delta < _TOLERANCE:
            break

    peak = max(rank.values())
    if peak <= 0.0:
        return {u: 0.0 for u in node_list}
    return {u: rank[u] / peak for u in node_list}


def _is_member(model: type[KnowledgeArtifact] | type[KnowledgeEdge], build_seq: int):
    """."""
    return (model.valid_from_seq <= build_seq) & (
        model.invalidated_at_seq.is_(None) | (model.invalidated_at_seq > build_seq)
    )


async def run_centrality(session: AsyncSession, *, build_seq: int) -> int:
    """Compute centrality over the live graph and write `centrality_score` per member artifact.

    Runs in `_finalize_graph` AFTER the invalidation pass (so it ranks the served, post-sweep set)
    and BEFORE activation, in the same pre-activation transaction. Returns the node count.
    """
    node_rows = (
        await session.execute(
            select(KnowledgeArtifact.artifact_id).where(_is_member(KnowledgeArtifact, build_seq))
        )
    ).scalars()
    nodes = {str(a) for a in node_rows}

    edge_rows = (
        await session.execute(
            select(KnowledgeEdge.from_artifact_id, KnowledgeEdge.to_artifact_id).where(
                _is_member(KnowledgeEdge, build_seq)
            )
        )
    ).all()
    edges = [(str(f), str(t)) for f, t in edge_rows]

    scores = pagerank(edges, nodes)
    if scores:
        await session.execute(
            update(KnowledgeArtifact),
            [
                {"artifact_id": uuid.UUID(node), "centrality_score": score}
                for node, score in scores.items()
            ],
        )
    logger.info(
        "event=centrality_computed build_seq=%d nodes=%d edges=%d",
        build_seq,
        len(nodes),
        len(edges),
    )
    return len(nodes)


__all__ = ["pagerank", "run_centrality"]
