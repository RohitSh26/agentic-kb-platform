"""Semantic fallback linking: embedding similarity behind a Protocol.

Runs only for concepts the deterministic pass could not link (deterministic
match wins). The provider is an interface over the vector store — no real
implementation until the Azure Search projection lands (PR-08) — so the build
passes None and the pass is skipped with a structured log. Confidence is the
raw similarity score, never inflated to look deterministic.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agentic_kb_builder.domain import LinkEdgeDraft
from agentic_kb_builder.linker.records import LinkableArtifact
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

SEMANTIC_ACCEPT_THRESHOLD = 0.82
SEMANTIC_TOP_K = 3


@dataclass(frozen=True)
class ScoredArtifact:
    artifact_id: uuid.UUID
    similarity: float


class SimilarityProvider(Protocol):
    async def similar_code_symbols(
        self, *, artifact_id: uuid.UUID, top_k: int
    ) -> Sequence[ScoredArtifact]:
        """Return code_symbol artifacts most similar to the given artifact."""
        ...


async def find_semantic_links(
    provider: SimilarityProvider,
    concepts: Sequence[LinkableArtifact],
    *,
    existing_pairs: set[tuple[uuid.UUID, uuid.UUID, str]],
) -> list[LinkEdgeDraft]:
    """Propose implements(symbol -> concept) edges for unlinked concepts.

    existing_pairs is mutated in place: accepted (from, to, edge_type) keys are
    added so deterministic edges always win and no pair is proposed twice.
    """
    drafts: list[LinkEdgeDraft] = []
    rejected = 0
    for concept in concepts:
        scored = await provider.similar_code_symbols(
            artifact_id=concept.artifact_id, top_k=SEMANTIC_TOP_K
        )
        for candidate in scored:
            if candidate.similarity < SEMANTIC_ACCEPT_THRESHOLD:
                rejected += 1
                continue
            key = (candidate.artifact_id, concept.artifact_id, "implements")
            if key in existing_pairs:
                continue
            existing_pairs.add(key)
            drafts.append(
                LinkEdgeDraft(
                    from_artifact_id=candidate.artifact_id,
                    to_artifact_id=concept.artifact_id,
                    edge_type="implements",
                    confidence=candidate.similarity,
                    strategy="semantic",
                )
            )
    logger.info(
        "event=linker_semantic_matched concepts=%d edges=%d rejected_below_threshold=%d",
        len(concepts),
        len(drafts),
        rejected,
    )
    return drafts
