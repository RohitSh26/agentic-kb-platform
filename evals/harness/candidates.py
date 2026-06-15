"""Candidate-generator quality metrics (PR-28, docs/contracts/relationship-candidates.md).

Candidate-then-judge is the architecture: a global LLM pass over every artifact pair is
O(N^2) and financially dead at scale. Before spending LLM tokens on judging (phase 3B), we
must prove the cheap, deterministic generator (kb-builder) has the RECALL to be worth judging,
and measure how many candidates it produces (VOLUME) and what it would cost to judge them all
(COST-IF-JUDGED).

This module is the pure, DB-free seam (ADR-0008: evals cannot import kb-builder). It scores a
set of generated candidate PAIRS against the cross-domain golden set's EXPECTED relations and
estimates judge cost. The numbers are reported by run.py and unit-tested without a database;
the real generator output is measured in kb-builder's integration test.
"""

from dataclasses import dataclass, field

# Estimated tokens to LLM-judge ONE candidate pair (prompt + bounded evidence + verdict).
# A simple per-candidate estimate * count gives cost-if-judged, so phase-3B affordability is
# decidable. Conservative round number; tune from real judge prompts when 3B lands.
JUDGE_TOKENS_PER_CANDIDATE = 1200


@dataclass(frozen=True)
class CandidatePair:
    """One generated cross-domain candidate: an UNORDERED artifact-key pair + whether a
    sampled reviewer judged it a real relationship (for sampled precision). is_relevant is
    None when the pair was not in the precision sample."""

    from_key: str
    to_key: str
    is_relevant: bool | None = None

    @property
    def unordered(self) -> frozenset[str]:
        return frozenset((self.from_key, self.to_key))


@dataclass(frozen=True)
class CandidateReport:
    """Phase-3A go/no-go numbers over a generated candidate set vs the golden expectations."""

    candidate_count: int
    from_artifacts: int
    expected_relations: int
    recall: float | None
    precision: float | None
    volume_per_artifact: float | None
    cost_if_judged_tokens: int
    missing_relations: tuple[frozenset[str], ...] = field(default_factory=tuple)


def candidate_recall(
    candidates: list[CandidatePair], expected_relations: list[frozenset[str]]
) -> float | None:
    """Fraction of expected cross-domain relations surfaced as a candidate (either
    direction). The headline phase-3A metric. None when there is nothing to score."""
    if not expected_relations:
        return None
    surfaced = {c.unordered for c in candidates}
    found = sum(1 for rel in expected_relations if rel in surfaced)
    return found / len(expected_relations)


def candidate_precision(candidates: list[CandidatePair]) -> float | None:
    """Sampled precision: of the candidates a reviewer judged, the fraction real. None
    when no candidate was sampled (is_relevant left None)."""
    sampled = [c for c in candidates if c.is_relevant is not None]
    if not sampled:
        return None
    return sum(1 for c in sampled if c.is_relevant) / len(sampled)


def volume_per_artifact(candidates: list[CandidatePair]) -> float | None:
    """Mean candidates per distinct `from` artifact (must stay <= the fan-out bound K)."""
    if not candidates:
        return None
    from_artifacts = {c.from_key for c in candidates}
    return len(candidates) / len(from_artifacts)


def cost_if_judged(candidates: list[CandidatePair]) -> int:
    """Estimated tokens to LLM-judge the whole candidate set (count * per-candidate)."""
    return len(candidates) * JUDGE_TOKENS_PER_CANDIDATE


def aggregate_candidates(
    candidates: list[CandidatePair], expected_relations: list[frozenset[str]]
) -> CandidateReport:
    """Fold the candidate set into the phase-3A report (the go/no-go inputs)."""
    surfaced = {c.unordered for c in candidates}
    missing = tuple(rel for rel in expected_relations if rel not in surfaced)
    return CandidateReport(
        candidate_count=len(candidates),
        from_artifacts=len({c.from_key for c in candidates}),
        expected_relations=len(expected_relations),
        recall=candidate_recall(candidates, expected_relations),
        precision=candidate_precision(candidates),
        volume_per_artifact=volume_per_artifact(candidates),
        cost_if_judged_tokens=cost_if_judged(candidates),
        missing_relations=missing,
    )


__all__ = [
    "JUDGE_TOKENS_PER_CANDIDATE",
    "CandidatePair",
    "CandidateReport",
    "aggregate_candidates",
    "candidate_precision",
    "candidate_recall",
    "cost_if_judged",
    "volume_per_artifact",
]
