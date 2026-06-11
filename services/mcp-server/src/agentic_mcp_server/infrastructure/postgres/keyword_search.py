"""Postgres keyword implementation of the SearchClient protocol.

The local/default relevance backend: scores knowledge_artifact rows by how
many query tokens hit the title (weight 2) or body (weight 1). Deliberately
simple — relevance is only a hint; Postgres remains the truth that hydrates
every card. Azure AI Search slots in behind the same protocol later.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.domain.query_text import normalize_query
from agentic_mcp_server.infrastructure.postgres.artifacts import KNOWLEDGE_ARTIFACT_TABLE
from agentic_mcp_server.infrastructure.search.search_client import SearchHit

_MAX_QUERY_TOKENS = 12


def _escape_like(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_query(token_count: int) -> str:
    score = " + ".join(
        f"(CASE WHEN title ILIKE :tok{i} ESCAPE '\\' THEN 2.0 ELSE 0.0 END"
        f" + CASE WHEN body_text ILIKE :tok{i} ESCAPE '\\' THEN 1.0 ELSE 0.0 END)"
        for i in range(token_count)
    )
    return (
        f"SELECT artifact_id, {score} AS score FROM {KNOWLEDGE_ARTIFACT_TABLE} "
        "WHERE kb_version = :kb_version "
        f"AND {score} > 0 ORDER BY score DESC, artifact_id LIMIT :top"
    )


@dataclass(frozen=True)
class PostgresKeywordSearchClient:
    session_factory: async_sessionmaker[AsyncSession]

    async def search(self, query: str, *, kb_version: str, top: int) -> list[SearchHit]:
        tokens = list(dict.fromkeys(normalize_query(query).split()))[:_MAX_QUERY_TOKENS]
        if not tokens:
            return []
        params: dict[str, object] = {"kb_version": kb_version, "top": top}
        for i, token in enumerate(tokens):
            params[f"tok{i}"] = f"%{_escape_like(token)}%"
        async with self.session_factory() as session:
            result = await session.execute(text(_build_query(len(tokens))), params)
            return [SearchHit(artifact_id=row.artifact_id, score=row.score) for row in result]
