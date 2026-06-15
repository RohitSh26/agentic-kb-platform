"""EmbeddingSimilarityProvider — the linker's SimilarityProvider, for real (ADR-0019).

Embeds every live artifact's text once (cache-gated via embedding_cache, so unchanged
text is never re-embedded) and answers `similar_code_symbols` by cosine nearest-neighbour
over the code_symbol corpus. This is the prose<->code bridge that was disabled while the
provider was None — concepts/tickets/docs now reach code by MEANING, not only exact name
matches.

The matrix maths is numpy (a single normalized matrix-vector product per query), so the
per-query cost stays flat as the corpus grows at nightly scale.
"""

import uuid

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.application.build_runner import Embedder, EmbeddingResult
from agentic_kb_builder.application.cache_gates import EmbeddingCacheGate
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import KnowledgeArtifact
from agentic_kb_builder.linker.semantic import ScoredArtifact
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Bound the embedded text so a huge body never blows the model's context or wastes
# tokens; the head of a code span / prose body carries the signal that matters.
_MAX_CHARS = 6000


def _compose_text(title: str | None, body_text: str | None) -> str | None:
    parts = [p for p in (title, body_text) if p]
    if not parts:
        return None
    return "\n".join(parts)[:_MAX_CHARS]


class EmbeddingSimilarityProvider:
    """SimilarityProvider Protocol impl over an Embedder + embedding_cache.

    Lazily prepares on first query (the linker runs after artifacts are written), so
    construction is cheap and embedding only happens when a real semantic pass runs.
    """

    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self._session = session
        self._embedder = embedder
        self._gate = EmbeddingCacheGate(session)
        self._prepared = False
        self._vectors: dict[uuid.UUID, np.ndarray] = {}  # all live artifacts, L2-normalized
        self._symbol_ids: list[uuid.UUID] = []  # code_symbol corpus order
        self._matrix: np.ndarray | None = None  # (len(symbol_ids), D), each row normalized

    async def _embed_cached(self, artifact_id: uuid.UUID, text: str) -> list[float]:
        text_hash = content_hash(text)
        model = self._embedder.embedding_model
        hit = await self._gate.lookup(
            artifact_id=artifact_id, text_hash=text_hash, embedding_model=model
        )
        if hit is not None and hit.embedding is not None:
            return list(hit.embedding)
        result: EmbeddingResult = await self._embedder.embed(text)
        await self._gate.record(
            artifact_id=artifact_id,
            text_hash=text_hash,
            embedding_model=model,
            embedding_hash=result.embedding_hash,
            embedding=result.vector,
        )
        return result.vector

    async def _prepare(self) -> None:
        if self._prepared:
            return
        rows = (
            await self._session.execute(
                select(
                    KnowledgeArtifact.artifact_id,
                    KnowledgeArtifact.artifact_type,
                    KnowledgeArtifact.title,
                    KnowledgeArtifact.body_text,
                ).where(KnowledgeArtifact.invalidated_at_seq.is_(None))
            )
        ).all()

        symbol_rows: list[np.ndarray] = []
        embedded = 0
        for artifact_id, artifact_type, title, body_text in rows:
            text = _compose_text(title, body_text)
            if text is None:
                continue
            vector = np.asarray(await self._embed_cached(artifact_id, text), dtype=np.float64)
            norm = float(np.linalg.norm(vector))
            if norm == 0.0:
                continue
            unit = vector / norm
            self._vectors[artifact_id] = unit
            embedded += 1
            if artifact_type == "code_symbol":
                self._symbol_ids.append(artifact_id)
                symbol_rows.append(unit)
        self._matrix = np.vstack(symbol_rows) if symbol_rows else None
        self._prepared = True
        logger.info(
            "event=embedding_similarity_prepared embedded=%d code_symbols=%d model=%s",
            embedded,
            len(self._symbol_ids),
            self._embedder.embedding_model,
        )

    async def similar_code_symbols(
        self, *, artifact_id: uuid.UUID, top_k: int
    ) -> list[ScoredArtifact]:
        await self._prepare()
        query = self._vectors.get(artifact_id)
        if query is None or self._matrix is None:
            return []
        scores = self._matrix @ query  # cosine: both sides L2-normalized
        # top_k+1 then drop self (a code_symbol querying itself scores 1.0).
        k = min(top_k + 1, scores.shape[0])
        top = np.argpartition(scores, -k)[-k:]
        ranked = top[np.argsort(scores[top])[::-1]]
        out: list[ScoredArtifact] = []
        for idx in ranked:
            sid = self._symbol_ids[int(idx)]
            if sid == artifact_id:
                continue
            out.append(ScoredArtifact(artifact_id=sid, similarity=float(scores[idx])))
            if len(out) == top_k:
                break
        return out


__all__ = ["EmbeddingSimilarityProvider"]
