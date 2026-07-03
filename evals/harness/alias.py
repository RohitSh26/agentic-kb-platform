"""Alias/Reference golden-set case shape + resolution metrics (PR-38, ADR-0030).

Pure, DB-free seam mirroring `harness/golden.py`: an `AliasCase` (one hand-verified
terse phrase + its expected target path(s), `evals/retrieval_cases/alias_golden_v1.yaml`)
plus the metric functions over an `AliasResult` (what a resolver actually returned for
that case). The resolver itself — kb-builder's `alias/resolve.py` against a live
Postgres alias index — is deliberately NOT imported here (ADR-0008: services are
self-contained, evals never depends on kb-builder internals): callers inject the
resolved target list and this module only scores it. Wired by:

- `scripts/eval_alias_resolution.py` — the full 25-case run against a locally built KB
  (kb-builder's venv resolves each query for real; this module scores the results).
- The hermetic 5-case subset lives in kb-builder's own test suite (it needs the real
  mining + resolve pipeline, which is hermetic there — no DB), not here.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class AliasCase(BaseModel):
    """One golden terse phrase + its hand-verified expected target path(s)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]{1,96}$")
    query: str = Field(min_length=1)
    expected_targets: list[str] = Field(min_length=1)
    provenance: str = Field(min_length=1)


class AliasSuite(BaseModel):
    """The committed golden file's top-level shape (one suite, many cases)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite: str = Field(min_length=1)
    cases: list[AliasCase] = Field(min_length=1)


@dataclass(frozen=True)
class AliasResult:
    """What a resolver returned for one case: `resolved_targets` is the winning
    alias's full ranked target-path list (top-1 = `resolved_targets[0]`), or `()`
    on a resolver miss (no alias matched above the fuzzy floor)."""

    case: AliasCase
    resolved_targets: tuple[str, ...] = ()


def top1_hit(result: AliasResult) -> bool:
    """True iff the resolver's first-ranked path is one of the case's expected
    targets. A resolver miss (`resolved_targets == ()`) is never a hit."""
    if not result.resolved_targets:
        return False
    return result.resolved_targets[0] in set(result.case.expected_targets)


@dataclass(frozen=True)
class AliasReport:
    """Aggregate golden-set metrics (brief target: top1_accuracy >= 0.80)."""

    cases: int
    hits: int
    top1_accuracy: float
    misses: tuple[str, ...]  # case ids whose top-1 was wrong or absent


def aggregate(results: list[AliasResult]) -> AliasReport:
    if not results:
        return AliasReport(cases=0, hits=0, top1_accuracy=0.0, misses=())
    hits = sum(1 for r in results if top1_hit(r))
    misses = tuple(r.case.id for r in results if not top1_hit(r))
    return AliasReport(
        cases=len(results), hits=hits, top1_accuracy=hits / len(results), misses=misses
    )


def load_alias_suite(path: Path) -> AliasSuite:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return AliasSuite.model_validate(raw)


__all__ = [
    "AliasCase",
    "AliasReport",
    "AliasResult",
    "AliasSuite",
    "aggregate",
    "load_alias_suite",
    "top1_hit",
]
