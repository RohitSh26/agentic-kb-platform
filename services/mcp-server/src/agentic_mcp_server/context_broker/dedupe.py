"""Query dedupe: exact-match on normalized text, semantic reuse via token cosine.

Deterministic on purpose — the broker makes no embedding calls in V1 (see
docs/contracts/mcp-tools-contract.md), so semantic similarity is a cosine over
token count vectors. The threshold is configurable (default 0.90, tuned from
ledger logs per .claude/rules/token-budgets.md).
"""

import math
from collections import Counter
from dataclasses import dataclass, field

from agentic_mcp_server.domain.query_text import normalize_query

__all__ = ["PastRetrieval", "QueryHistory", "normalize_query", "similarity"]


def _token_counts(normalized_query: str) -> Counter[str]:
    return Counter(normalized_query.split())


def similarity(normalized_a: str, normalized_b: str) -> float:
    """Cosine similarity over token count vectors of two normalized queries."""
    a, b = _token_counts(normalized_a), _token_counts(normalized_b)
    if not a or not b:
        return 0.0
    dot = sum(count * b[token] for token, count in a.items())
    norm = math.sqrt(sum(c * c for c in a.values())) * math.sqrt(sum(c * c for c in b.values()))
    return dot / norm if norm else 0.0


@dataclass(frozen=True)
class PastRetrieval:
    normalized_query: str
    evidence_ids: tuple[str, ...]


@dataclass
class QueryHistory:
    """Per-pack record of what was already asked and what evidence answered it."""

    entries: list[PastRetrieval] = field(default_factory=list)

    def find_exact(self, normalized_query: str) -> PastRetrieval | None:
        for entry in self.entries:
            if entry.normalized_query == normalized_query:
                return entry
        return None

    def find_semantic(self, normalized_query: str, threshold: float) -> PastRetrieval | None:
        best: PastRetrieval | None = None
        best_score = 0.0
        for entry in self.entries:
            score = similarity(normalized_query, entry.normalized_query)
            if score >= threshold and score > best_score:
                best, best_score = entry, score
        return best

    def record(self, normalized_query: str, evidence_ids: list[str]) -> None:
        self.entries.append(PastRetrieval(normalized_query, tuple(evidence_ids)))
