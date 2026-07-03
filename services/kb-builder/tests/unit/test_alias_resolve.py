"""Pure alias resolution: terse query -> ranked target paths (PR-38, alias/resolve.py).

Hermetic, no DB: exact-phrase match, fuzzy Jaccard match above/below the floor,
and deterministic tie-breaking (score desc, confirmation_count desc, alias asc).
"""

from agentic_kb_builder.alias.resolve import MIN_FUZZY_SCORE, AliasEntry, resolve


def _entry(alias: str, *targets: str, confirmation_count: int = 1) -> AliasEntry:
    return AliasEntry(alias=alias, targets=targets, confirmation_count=confirmation_count)


def test_exact_normalized_match_wins_with_score_one() -> None:
    entries = [_entry("durable model output cache", "durable_output_cache.py")]
    result = resolve("the durable model output cache", entries)
    assert result is not None
    assert result.matched == "exact"
    assert result.score == 1.0
    assert result.targets == ("durable_output_cache.py",)


def test_fuzzy_match_above_floor_returns_the_alias() -> None:
    entries = [_entry("graph centrality ranking", "centrality.py")]
    # "graph centrality" (2 tokens) vs "graph centrality ranking" (3 tokens):
    # intersection=2, union=3 -> 0.667, comfortably above the 0.3 floor.
    result = resolve("graph centrality", entries)
    assert result is not None
    assert result.matched == "fuzzy"
    assert result.targets == ("centrality.py",)


def test_no_match_below_floor_returns_none() -> None:
    entries = [_entry("graph centrality ranking", "centrality.py")]
    result = resolve("completely unrelated query text", entries)
    assert result is None


def test_empty_normalized_query_returns_none() -> None:
    # "the a an" normalizes to "" (all stopwords) — nothing to match on.
    entries = [_entry("graph centrality ranking", "centrality.py")]
    assert resolve("the a an", entries) is None


def test_no_entries_returns_none() -> None:
    assert resolve("anything", []) is None


def test_tie_break_prefers_higher_confirmation_count() -> None:
    entries = [
        _entry("release scrub pipeline", "weak.py", confirmation_count=1),
        _entry("release scrub", "strong.py", confirmation_count=3),
    ]
    # both alias phrases are subsets of the query with identical Jaccard score
    # against "release scrub pipeline fix" — confirmation_count breaks the tie.
    result = resolve("release scrub", entries)
    assert result is not None
    assert result.targets == ("strong.py",)


def test_tie_break_prefers_alphabetically_first_alias_when_fully_tied() -> None:
    entries = [
        _entry("zzz release scrub", "z.py", confirmation_count=1),
        _entry("aaa release scrub", "a.py", confirmation_count=1),
    ]
    result = resolve("release scrub", entries)
    assert result is not None
    assert result.targets == ("a.py",)


def test_min_fuzzy_score_floor_is_030() -> None:
    assert MIN_FUZZY_SCORE == 0.3


def test_resolve_query_is_normalized_same_as_mining() -> None:
    entries = [_entry("idf weight query tokens", "keyword_search.py")]
    result = resolve("IDF-Weight Query Tokens!", entries)
    assert result is not None
    assert result.matched == "exact"
