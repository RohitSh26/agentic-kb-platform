"""Cross-domain relationship candidate generator.

The cheap, deterministic, ZERO-LLM stage of candidate-then-judge
(docs/contracts/relationship-candidates.md). For cross-domain artifact pairs that
are NOT already deterministically linked, it emits *candidates* — pairs worth the
phase-3B LLM judge looking at — using cheap signals only:

- embedding_similarity (via the linker's SimilarityProvider Protocol; None-safe —
  the build passes None until the vector projection lands, and the signal simply
  does not fire, so we never call a model endpoint directly),
- token_overlap (name/path/symbol token Jaccard),
- section_proximity (a doc/card body verbatim-references the other's path/symbol),
- path_colocation (shared directory prefix = code-ownership co-location).

It writes ONLY to relationship_candidate, NEVER to knowledge_edge, and calls NO
LLM. Fan-out is BOUNDED: each `from` artifact keeps only its top-K candidates
(CANDIDATE_FAN_OUT_K) — no O(N^2) full cross-product. Deterministic for fixed
inputs: stable ordering, stable tie-breaks on (-score, to_artifact_id).
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from agentic_kb_builder.linker.records import (
    CARD_SOURCE_TYPES,
    CODE_ARTIFACT_TYPES,
    COMMIT_SOURCE_TYPES,
    DOC_SOURCE_TYPES,
    LinkableArtifact,
)
from agentic_kb_builder.linker.semantic import SimilarityProvider
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Bounded fan-out: each `from` artifact keeps at most this many candidates, so the
# total candidate count is bounded by K * |artifacts| — never the full cross-product
# (no O(N^2)). Tune from the volume/cost metrics; record a change in the contract.
CANDIDATE_FAN_OUT_K = 10

# Signal score floors. A signal below its floor does not fire (its key is absent
# from `signals`). The embedding floor was raised 0.70 -> 0.80: at
# 0.70, weak code_symbol↔concept pairs reached the judge and were labelled INFERRED_LOW
# at ~50% precision (the dominant noise source); 0.80 keeps only strong nomic matches,
# cutting that noise without losing the high-confidence cross-domain links.
EMBEDDING_SIMILARITY_FLOOR = 0.80
TOKEN_OVERLAP_FLOOR = 0.10
SECTION_PROXIMITY_SCORE = 0.6
PATH_COLOCATION_SCORE = 0.4

# A candidate's recall bucket from its summary score (audit only; the judge re-scores).
HIGH_BUCKET_FLOOR = 0.7
MEDIUM_BUCKET_FLOOR = 0.4

# top-K neighbours requested from the embedding provider per artifact.
EMBEDDING_TOP_K = 5

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class CandidateDraft:
    """One relationship_candidate row: a cross-domain pair + the signals that fired."""

    from_artifact_id: uuid.UUID
    to_artifact_id: uuid.UUID
    signals: dict[str, float]
    candidate_recall_bucket: str

    @property
    def score(self) -> float:
        """Audit summary score: the strongest firing signal (the judge re-scores)."""
        return max(self.signals.values()) if self.signals else 0.0


@dataclass
class _Scratch:
    """Accumulating signals for one ordered (from, to) pair before we finalise it."""

    signals: dict[str, float] = field(default_factory=dict)

    def offer(self, name: str, score: float) -> None:
        # keep the strongest observation of a signal for a pair (deterministic).
        if score > self.signals.get(name, 0.0):
            self.signals[name] = score


def artifact_domain(artifact: LinkableArtifact) -> str:
    """Coarse domain of an artifact for cross-domain pairing.

    code (code source types / code artifact types), doc (wiki/github_doc), card
    (ado_card work-items), commit (git_metadata). Unknown ⇒ 'other'.
    """
    if artifact.source_type in COMMIT_SOURCE_TYPES:
        return "commit"
    if artifact.source_type in CARD_SOURCE_TYPES:
        return "card"
    if artifact.source_type in DOC_SOURCE_TYPES:
        return "doc"
    if artifact.artifact_type in CODE_ARTIFACT_TYPES or artifact.path is not None:
        return "code"
    return "other"


def _tokens(*parts: str | None) -> frozenset[str]:
    out: set[str] = set()
    for part in parts:
        if not part:
            continue
        out.update(t.lower() for t in _TOKEN_PATTERN.findall(part) if len(t) >= 3)
    return frozenset(out)


def _token_overlap(a_tokens: frozenset[str], b_tokens: frozenset[str]) -> float:
    if not a_tokens or not b_tokens:
        return 0.0
    inter = len(a_tokens & b_tokens)
    if inter == 0:
        return 0.0
    return inter / len(a_tokens | b_tokens)


def _dir_prefix(path: str | None) -> str | None:
    if not path or "/" not in path:
        return None
    return path.rsplit("/", 1)[0]


def _bucket(score: float) -> str:
    if score >= HIGH_BUCKET_FLOOR:
        return "high"
    if score >= MEDIUM_BUCKET_FLOOR:
        return "medium"
    return "low"


async def generate_candidates(
    artifacts: Sequence[LinkableArtifact],
    *,
    existing_pairs: frozenset[frozenset[uuid.UUID]],
    similarity: SimilarityProvider | None = None,
) -> list[CandidateDraft]:
    """Emit bounded, deterministic cross-domain candidates.

    existing_pairs is the set of UNORDERED artifact-id pairs already linked
    deterministically (a live knowledge_edge with source='linker') — those are
    excluded (the judge never re-judges a deterministic fact). similarity is the
    embedding provider; None-safe (the embedding signal simply does not fire).
    """
    by_id = {a.artifact_id: a for a in artifacts}
    token_cache = {a.artifact_id: _tokens(a.title, a.path, a.external_id) for a in artifacts}
    # accumulate per ordered (from, to) pair, deterministically.
    scratch: dict[tuple[uuid.UUID, uuid.UUID], _Scratch] = {}

    def offer(from_id: uuid.UUID, to_id: uuid.UUID, name: str, score: float) -> None:
        if from_id == to_id:
            return
        if frozenset((from_id, to_id)) in existing_pairs:
            return
        if artifact_domain(by_id[from_id]) == artifact_domain(by_id[to_id]):
            return
        scratch.setdefault((from_id, to_id), _Scratch()).offer(name, score)

    # Deterministic O(N^2)-free-at-write fan-out is enforced after scoring; the
    # signal sweep itself is bounded per artifact (embedding top-K) or cheap pairwise
    # over the candidate set we already hold in memory (nightly scale, V1 bound).
    embedding_fired = 0
    if similarity is not None:
        for artifact in sorted(artifacts, key=lambda a: a.artifact_id):
            scored = await similarity.similar_code_symbols(
                artifact_id=artifact.artifact_id, top_k=EMBEDDING_TOP_K
            )
            for cand in scored:
                if cand.similarity < EMBEDDING_SIMILARITY_FLOOR:
                    continue
                if cand.artifact_id not in by_id:
                    continue
                offer(
                    artifact.artifact_id,
                    cand.artifact_id,
                    "embedding_similarity",
                    cand.similarity,
                )
                embedding_fired += 1
    else:
        logger.info("event=candidate_embedding_skipped reason=no_provider")

    # token / section / path signals: pairwise over artifacts, stable ordering.
    ordered = sorted(artifacts, key=lambda a: a.artifact_id)
    for from_a in ordered:
        from_tokens = token_cache[from_a.artifact_id]
        from_dir = _dir_prefix(from_a.path)
        body_lower = (from_a.body_text or "").lower()
        for to_a in ordered:
            if to_a.artifact_id == from_a.artifact_id:
                continue
            overlap = _token_overlap(from_tokens, token_cache[to_a.artifact_id])
            if overlap >= TOKEN_OVERLAP_FLOOR:
                offer(from_a.artifact_id, to_a.artifact_id, "token_overlap", overlap)
            # section proximity: from_a (a doc/card body) names the other's path/symbol.
            needle = (to_a.path or to_a.title or "").lower()
            if len(needle) >= 4 and body_lower and needle in body_lower:
                offer(
                    from_a.artifact_id,
                    to_a.artifact_id,
                    "section_proximity",
                    SECTION_PROXIMITY_SCORE,
                )
            # path co-location: both carry a path under the same directory.
            to_dir = _dir_prefix(to_a.path)
            if from_dir is not None and from_dir == to_dir:
                offer(
                    from_a.artifact_id,
                    to_a.artifact_id,
                    "path_colocation",
                    PATH_COLOCATION_SCORE,
                )

    drafts = [
        CandidateDraft(
            from_artifact_id=from_id,
            to_artifact_id=to_id,
            signals=dict(sorted(s.signals.items())),
            candidate_recall_bucket=_bucket(max(s.signals.values()) if s.signals else 0.0),
        )
        for (from_id, to_id), s in scratch.items()
        if s.signals
    ]
    bounded = _bound_fan_out(drafts)
    logger.info(
        "event=candidate_generated artifacts=%d raw_pairs=%d candidates=%d "
        "fan_out_k=%d embedding_signals=%d",
        len(artifacts),
        len(drafts),
        len(bounded),
        CANDIDATE_FAN_OUT_K,
        embedding_fired,
    )
    return bounded


def _bound_fan_out(drafts: Sequence[CandidateDraft]) -> list[CandidateDraft]:
    """Keep at most CANDIDATE_FAN_OUT_K candidates per `from` artifact.

    Deterministic: drafts for a `from` are sorted by (-score, to_artifact_id) and
    truncated. The total is bounded by K * |from artifacts| — never O(N^2).
    """
    grouped: dict[uuid.UUID, list[CandidateDraft]] = {}
    for draft in drafts:
        grouped.setdefault(draft.from_artifact_id, []).append(draft)
    kept: list[CandidateDraft] = []
    dropped = 0
    for from_id in sorted(grouped):
        ranked = sorted(grouped[from_id], key=lambda d: (-d.score, str(d.to_artifact_id)))
        kept.extend(ranked[:CANDIDATE_FAN_OUT_K])
        dropped += max(0, len(ranked) - CANDIDATE_FAN_OUT_K)
    if dropped:
        logger.info("event=candidate_fan_out_bounded dropped=%d k=%d", dropped, CANDIDATE_FAN_OUT_K)
    # Global stable order so the written set is deterministic.
    return sorted(kept, key=lambda d: (str(d.from_artifact_id), str(d.to_artifact_id)))


__all__ = [
    "CANDIDATE_FAN_OUT_K",
    "CandidateDraft",
    "artifact_domain",
    "generate_candidates",
]
