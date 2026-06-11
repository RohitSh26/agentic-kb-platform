"""Query-side SearchClient interface + in-memory fake.

The broker depends only on this Protocol; relevance backends stay swappable
(Postgres keyword search locally, Azure AI Search behind the same seam later —
the import-boundary test still forbids the azure SDK here). Search results are
hints only: every returned artifact is re-read from Postgres, the source of
truth, before anything reaches an agent.
"""

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from agentic_mcp_server.domain.query_text import normalize_query


@dataclass(frozen=True)
class SearchHit:
    artifact_id: uuid.UUID
    score: float


class SearchClient(Protocol):
    async def search(self, query: str, *, kb_version: str, top: int) -> list[SearchHit]:
        """Return up to `top` relevance hits for the active kb_version."""
        ...


@dataclass
class FakeSearchClient:
    """Keyword-seeded fake for tests and the local development loop."""

    hits_by_keyword: dict[str, list[SearchHit]] = field(default_factory=dict)

    def seed(self, keyword: str, hits: list[SearchHit]) -> None:
        self.hits_by_keyword[normalize_query(keyword)] = hits

    async def search(self, query: str, *, kb_version: str, top: int) -> list[SearchHit]:
        tokens = set(normalize_query(query).split())
        best: dict[uuid.UUID, SearchHit] = {}
        for keyword, hits in self.hits_by_keyword.items():
            if keyword in tokens:
                for hit in hits:
                    current = best.get(hit.artifact_id)
                    if current is None or hit.score > current.score:
                        best[hit.artifact_id] = hit
        ranked = sorted(best.values(), key=lambda hit: hit.score, reverse=True)
        return ranked[:top]
