"""Pure alias resolution (PR-38): terse query -> ranked target paths.

Hermetic and deterministic — no DB, no model. The query is normalized with the
SAME tokenizer as mining (docs/contracts/alias-reference.md "Resolution"): an
exact normalized match wins outright; otherwise the best token-set Jaccard
overlap above a floor, tie-broken by confirmation_count desc then phrase asc.
The winning alias's already-ranked targets are the answer (top-1 = targets[0]).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from agentic_kb_builder.alias.mining import normalize_phrase

# Below this token-set Jaccard the match is noise, not an alias hit — the caller
# should fall through to ordinary search instead of guessing (proposal §1).
MIN_FUZZY_SCORE = 0.3


@dataclass(frozen=True)
class AliasEntry:
    """One live alias_reference row, reduced to what resolution needs."""

    alias: str  # normalized phrase (knowledge_artifact.title)
    targets: tuple[str, ...]  # ranked target paths (body JSON order)
    confirmation_count: int = 1


@dataclass(frozen=True)
class Resolution:
    alias: str
    score: float
    matched: Literal["exact", "fuzzy"]
    targets: tuple[str, ...]


def resolve(query: str, entries: Sequence[AliasEntry]) -> Resolution | None:
    """Resolve a terse developer phrase against the alias index, or None.

    Returning None (rather than a bad guess) is the contract: ambiguity below
    the floor is surfaced, never silently resolved (invariant 7).
    """
    normalized = normalize_phrase(query)
    if not normalized:
        return None
    query_tokens = frozenset(normalized.split())
    best: tuple[float, int, str] | None = None
    best_entry: AliasEntry | None = None
    for entry in entries:
        if entry.alias == normalized:
            return Resolution(alias=entry.alias, score=1.0, matched="exact", targets=entry.targets)
        alias_tokens = frozenset(entry.alias.split())
        union = query_tokens | alias_tokens
        if not union:
            continue
        score = len(query_tokens & alias_tokens) / len(union)
        if score < MIN_FUZZY_SCORE:
            continue
        # Rank: score desc, confirmation desc, alias asc (deterministic).
        key = (score, entry.confirmation_count, entry.alias)
        if (
            best is None
            or (key[0], key[1]) > (best[0], best[1])
            or ((key[0], key[1]) == (best[0], best[1]) and key[2] < best[2])
        ):
            best = key
            best_entry = entry
    if best is None or best_entry is None:
        return None
    return Resolution(
        alias=best_entry.alias, score=best[0], matched="fuzzy", targets=best_entry.targets
    )


__all__ = ["MIN_FUZZY_SCORE", "AliasEntry", "Resolution", "resolve"]
