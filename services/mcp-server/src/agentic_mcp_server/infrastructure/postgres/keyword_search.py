"""Postgres keyword implementation of the SearchClient protocol.

The local/default relevance backend: scores knowledge_artifact rows by how many
query tokens hit the title/symbol name (weight 4), search_text (1.5, the curated code
retrieval surface) or body (weight 1). Each token is additionally weighted by its
INVERSE DOCUMENT FREQUENCY: a token that appears in few artifacts (e.g. ``graphify``)
outweighs a common one (``graph``/``code``/``create``), so a distinctive term in the
question dominates generic overlap and the pack stays on-topic.

The title weight is deliberately large (4 vs body's 1): an artifact whose TITLE /
symbol name literally IS the thing asked about (``BudgetPolicy`` for "per-agent token
budget") is the answer, whereas a row that merely mentions the term in its body is
context. So a single distinctive-term title hit must outrank a row that stacks several
incidental body mentions plus a common title term — otherwise the symbol that defines
the concept gets buried under files that only reference it. IDF still applies, so a
title hit on a rare term beats a title hit on a generic one. Relevance is only a hint;
Postgres remains the truth that hydrates every card. Azure AI Search slots in behind
the same protocol later.
"""

import math
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.domain.query_text import normalize_query
from agentic_mcp_server.infrastructure.postgres.artifacts import KNOWLEDGE_ARTIFACT_TABLE
from agentic_mcp_server.infrastructure.search.search_client import SearchHit

_MAX_QUERY_TOKENS = 12

# Membership predicate (version-membership.md, ADR-0013): a candidate row must be a
# MEMBER of the active build_seq, not merely labelled with its kb_version.
_MEMBER = (
    "valid_from_seq <= :build_seq "
    "AND (invalidated_at_seq IS NULL OR invalidated_at_seq > :build_seq)"
)


def _escape_like(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _matches(i: int) -> str:
    """SQL: does token i hit any searchable field of the row?"""
    return (
        f"(title ILIKE :tok{i} ESCAPE '\\' "
        f"OR search_text ILIKE :tok{i} ESCAPE '\\' "
        f"OR body_text ILIKE :tok{i} ESCAPE '\\')"
    )


def _df_query(token_count: int) -> str:
    """Total member artifacts (n) and per-token document frequency (df{i}), one scan."""
    dfs = ", ".join(
        f"coalesce(sum(CASE WHEN {_matches(i)} THEN 1 ELSE 0 END), 0) AS df{i}"
        for i in range(token_count)
    )
    return f"SELECT count(*) AS n, {dfs} FROM {KNOWLEDGE_ARTIFACT_TABLE} WHERE {_MEMBER}"


def _build_query(token_count: int) -> str:
    # Field weights: title 4.0 > search_text 1.5 > body_text 1.0. The title/symbol-name
    # weight is deliberately several times the body weight so an artifact whose name IS the
    # query term outranks one that only mentions it in prose, even when the prose row stacks
    # multiple incidental matches (the "BudgetPolicy vs ChangeContext*" failure). search_text
    # (PR-34) is the deterministic retrieval surface for code symbols (split identifiers,
    # docstring words, signatures, called names) — it carries the concept words a task names
    # that a raw span misses, so it scores ABOVE body_text but below title. NULL search_text
    # (non-code / span-less) simply never matches (ELSE 0.0). Each token's field score is
    # scaled by its IDF weight :w{i} (computed from _df_query), so a distinctive title term
    # still beats a common one.
    score = " + ".join(
        f":w{i} * (CASE WHEN title ILIKE :tok{i} ESCAPE '\\' THEN 4.0 ELSE 0.0 END"
        f" + CASE WHEN search_text ILIKE :tok{i} ESCAPE '\\' THEN 1.5 ELSE 0.0 END"
        f" + CASE WHEN body_text ILIKE :tok{i} ESCAPE '\\' THEN 1.0 ELSE 0.0 END)"
        for i in range(token_count)
    )
    # The membership filter is repeated (not the IDF weights) so an unweighted-but-matching
    # row is still bounded by build_seq; the > 0 guard drops non-matching rows.
    raw = " + ".join(
        f"(CASE WHEN title ILIKE :tok{i} ESCAPE '\\' THEN 4.0 ELSE 0.0 END"
        f" + CASE WHEN search_text ILIKE :tok{i} ESCAPE '\\' THEN 1.5 ELSE 0.0 END"
        f" + CASE WHEN body_text ILIKE :tok{i} ESCAPE '\\' THEN 1.0 ELSE 0.0 END)"
        for i in range(token_count)
    )
    return (
        f"SELECT artifact_id, {score} AS score FROM {KNOWLEDGE_ARTIFACT_TABLE} "
        f"WHERE {_MEMBER} AND ({raw}) > 0 ORDER BY score DESC, artifact_id LIMIT :top"
    )


@dataclass(frozen=True)
class PostgresKeywordSearchClient:
    session_factory: async_sessionmaker[AsyncSession]

    async def search(self, query: str, *, build_seq: int, top: int) -> list[SearchHit]:
        tokens = list(dict.fromkeys(normalize_query(query).split()))[:_MAX_QUERY_TOKENS]
        if not tokens:
            return []
        like = {f"tok{i}": f"%{_escape_like(token)}%" for i, token in enumerate(tokens)}
        base: dict[str, object] = {"build_seq": build_seq, **like}
        async with self.session_factory() as session:
            df_row = (await session.execute(text(_df_query(len(tokens))), base)).one()
            total = int(df_row.n)
            if total == 0:
                return []
            # Smoothed IDF: log((N+1)/(df+1)) + 1. A token everywhere (df≈N) → ~1.0;
            # a rare token (df small) → larger, so it dominates the score.
            weights: dict[str, object] = {}
            for i in range(len(tokens)):
                df = int(getattr(df_row, f"df{i}"))
                weights[f"w{i}"] = math.log((total + 1) / (df + 1)) + 1.0
            params = {**base, **weights, "top": top}
            result = await session.execute(text(_build_query(len(tokens))), params)
            # Postgres NUMERIC arithmetic returns Decimal; the SearchClient contract
            # (and FakeSearchClient) yields float, and the ranker multiplies the score
            # by a float temporal weight — so coerce here or Decimal*float raises.
            return [
                SearchHit(artifact_id=row.artifact_id, score=float(row.score)) for row in result
            ]
