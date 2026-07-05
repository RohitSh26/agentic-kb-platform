"""The graph-centrality prior in the broker rank key (ADR-0028 / PR-36).

Centrality lifts the relevance term but never overrides the source_backed / authority tiers, and a
NULL/0 centrality reproduces the pre-PR-36 ordering (backward-safe). Pure-function tests (no DB).
"""

import uuid

from agentic_mcp_server.context_broker.retrieval import _CENTRALITY_BETA, _rank_key
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow


def _row(
    *,
    knowledge_kind: str = "interpreted",
    authority: float | None = 0.5,
    centrality: float | None = None,
) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=uuid.uuid4(),
        artifact_type="code_symbol",
        title="t",
        body_text="b",
        knowledge_kind=knowledge_kind,
        authority_score=authority,
        centrality_score=centrality,
        source_uri="u",
    )


def test_centrality_lifts_an_equal_relevance_artifact() -> None:
    high = _row(centrality=0.9)
    low = _row(centrality=0.0)
    scores = {high.artifact_id: 1.0, low.artifact_id: 1.0}  # identical keyword score
    assert _rank_key(high, scores, {}) > _rank_key(low, scores, {})


def test_null_centrality_reproduces_today_ordering() -> None:
    # with no centrality the relevance term is exactly base_score * weight (weight=1.0, temporal={})
    row = _row(centrality=None)
    key = _rank_key(row, {row.artifact_id: 0.7}, {})
    assert key[2] == 0.7  # the relevance element is unchanged: factor is (1 + beta*0) = 1


def test_centrality_factor_is_the_documented_multiplier() -> None:
    row = _row(centrality=1.0)
    key = _rank_key(row, {row.artifact_id: 1.0}, {})
    assert key[2] == 1.0 * (1.0 + _CENTRALITY_BETA * 1.0)


def test_source_backed_tier_dominates_centrality() -> None:
    # a source_backed artifact with ZERO centrality must still outrank an interpreted one with MAX
    # centrality — provenance is tuple element 0, centrality only affects element 2.
    backed = _row(knowledge_kind="source_backed", authority=0.0, centrality=0.0)
    interpreted = _row(knowledge_kind="interpreted", authority=0.9, centrality=1.0)
    scores = {backed.artifact_id: 0.1, interpreted.artifact_id: 1.0}
    assert _rank_key(backed, scores, {}) > _rank_key(interpreted, scores, {})
