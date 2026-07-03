"""Deterministic alias-phrase extraction + aggregation (PR-38, alias/mining.py).

Pure-function tests (no DB): tokenize/normalize, conventional-commit scope
parsing, stopword-bounded n-grams, doc filename slugs, and cross-source
aggregation (confirmation_count + ranked targets). docs/contracts/alias-reference.md.
"""

from agentic_kb_builder.alias.mining import (
    MinedPhrase,
    SourceContribution,
    aggregate_contributions,
    doc_slug_phrase,
    is_test_path,
    mine_commit,
    mine_doc_source,
    ngram_phrases,
    normalize_phrase,
    parse_subject_label,
    phrase_variants,
    tokenize,
)


def test_tokenize_lowercases_and_drops_short_fragments() -> None:
    assert tokenize("The Source's Cache-Fix v2") == ["the", "source", "cache", "fix", "v2"]


def test_normalize_phrase_strips_stopwords() -> None:
    assert normalize_phrase("the durable model output cache") == "durable model output cache"
    assert normalize_phrase("Fix the Bug") == "fix bug"


def test_normalize_phrase_of_all_stopwords_is_empty() -> None:
    assert normalize_phrase("the a an") == ""


def test_phrase_variants_hyphen_and_underscore_joins() -> None:
    assert phrase_variants("durable model cache") == (
        "durable-model-cache",
        "durable_model_cache",
    )


def test_phrase_variants_single_token_has_no_variants() -> None:
    assert phrase_variants("cache") == ()


def test_parse_subject_label_conventional_commit_with_scope() -> None:
    scope, desc = parse_subject_label("feat(kb-builder): durable output cache")
    assert scope == "kb-builder"
    assert desc == "durable output cache"


def test_parse_subject_label_conventional_commit_without_scope() -> None:
    scope, desc = parse_subject_label("fix: close the leak")
    assert scope is None
    assert desc == "close the leak"


def test_parse_subject_label_non_conventional_label_becomes_scope() -> None:
    scope, desc = parse_subject_label("docify: fix mapping edge case")
    assert scope == "docify"
    assert desc == "fix mapping edge case"


def test_parse_subject_label_no_label_at_all() -> None:
    scope, desc = parse_subject_label("Smoke client exercises context.expand")
    assert scope is None
    assert desc == "Smoke client exercises context.expand"


def test_ngram_phrases_covers_2_to_4_word_windows() -> None:
    phrases = ngram_phrases("durable model output cache")
    assert "durable model" in phrases
    assert "model output" in phrases
    assert "durable model output" in phrases
    assert "durable model output cache" in phrases
    # a 5th token would push a 4-gram out of range; here the run is exactly 4
    # tokens so no 5-word phrase exists at all.
    assert all(len(p.split()) <= 4 for p in phrases)


def test_ngram_phrases_stopword_breaks_the_window() -> None:
    # "the" splits the run into two 1-token islands: no 2..4-gram spans it.
    phrases = ngram_phrases("cache the fix")
    assert "cache the" not in phrases
    assert "the fix" not in phrases
    assert not phrases  # both islands are length 1, below NGRAM_MIN


def test_doc_slug_phrase_strips_pr_adr_and_numeric_tokens() -> None:
    assert (
        doc_slug_phrase("docs/pr-briefs/PR-38-alias-reference-index.md") == "alias reference index"
    )
    assert (
        doc_slug_phrase("docs/adr/0030-twelve-role-roster-and-langgraph-backend.md")
        == "twelve role roster langgraph backend"
    )


def test_doc_slug_phrase_none_for_non_markdown() -> None:
    assert doc_slug_phrase("docs/pr-briefs/PR-38-alias-reference-index.py") is None


def test_doc_slug_phrase_none_when_below_min_tokens() -> None:
    # after stripping the leading numeric token, only one token ("overview") is
    # left — not alias-worthy on its own.
    assert doc_slug_phrase("docs/architecture/00-overview.md") is None


def test_is_test_path_matches_dir_and_filename_prefix() -> None:
    assert is_test_path("services/kb-builder/tests/unit/test_mining.py") is True
    assert is_test_path("scripts/test_kb_agent_safety.py") is True
    assert is_test_path("services/kb-builder/src/alias/mining.py") is False


def test_mine_commit_scope_and_ngrams_target_changed_files() -> None:
    phrases = mine_commit(
        "feat(kb-builder): durable output cache",
        (
            "services/kb-builder/src/agentic_kb_builder/infrastructure/postgres/durable_output_cache.py",
        ),
    )
    by_phrase = {p.phrase: p.targets for p in phrases}
    assert "kb builder" in by_phrase  # scope token
    assert "durable output cache" in by_phrase  # full n-gram
    assert all(
        targets
        == (
            "services/kb-builder/src/agentic_kb_builder/infrastructure/postgres/durable_output_cache.py",
        )
        for targets in by_phrase.values()
    )


def test_mine_commit_doc_slug_targets_only_that_file() -> None:
    phrases = mine_commit(
        "docs: adr-0027 + pr-35 brief",
        (
            "docs/adr/0027-crash-durable-model-output-cache.md",
            "docs/pr-briefs/PR-35-crash-durable-model-output-cache.md",
        ),
    )
    by_phrase = {p.phrase: p.targets for p in phrases}
    assert by_phrase["crash durable model output cache"] == (
        "docs/adr/0027-crash-durable-model-output-cache.md",
        "docs/pr-briefs/PR-35-crash-durable-model-output-cache.md",
    )


def test_mine_commit_no_files_mines_nothing() -> None:
    assert mine_commit("feat(kb-builder): durable output cache", ()) == ()


def test_mine_doc_source_targets_its_own_path() -> None:
    phrases = mine_doc_source("docs/adr/0025-kb-first-file-fallback.md")
    assert phrases == (
        MinedPhrase(
            phrase="kb first file fallback", targets=("docs/adr/0025-kb-first-file-fallback.md",)
        ),
    )


def test_mine_doc_source_none_below_min_tokens_yields_no_phrase() -> None:
    assert mine_doc_source("docs/architecture/00-overview.md") == ()


def test_aggregate_contributions_confirmation_count_across_sources() -> None:
    contributions = [
        SourceContribution(
            source_key="commit:git:sha1",
            ref="sha1",
            content_hash="h1",
            phrases=(
                MinedPhrase(phrase="human approval delegation gate", targets=("docs/adr/0021.md",)),
            ),
        ),
        SourceContribution(
            source_key="commit:git:sha2",
            ref="sha2",
            content_hash="h2",
            phrases=(
                MinedPhrase(phrase="human approval delegation gate", targets=("docs/adr/0021.md",)),
            ),
        ),
    ]
    (aggregate,) = aggregate_contributions(contributions)
    assert aggregate.phrase == "human approval delegation gate"
    assert aggregate.confirmation_count == 2
    assert aggregate.targets[0].path == "docs/adr/0021.md"
    assert aggregate.targets[0].count == 2


def test_aggregate_contributions_ranks_targets_by_frequency_then_non_test() -> None:
    contributions = [
        SourceContribution(
            source_key="commit:git:sha1",
            ref="sha1",
            content_hash="h1",
            phrases=(
                MinedPhrase(
                    phrase="path traversal sandbox bypass",
                    targets=("scripts/kb_agent.py", "scripts/test_kb_agent_safety.py"),
                ),
            ),
        ),
        SourceContribution(
            source_key="commit:git:sha2",
            ref="sha2",
            content_hash="h2",
            phrases=(
                MinedPhrase(
                    phrase="path traversal sandbox bypass", targets=("scripts/kb_agent.py",)
                ),
            ),
        ),
    ]
    (aggregate,) = aggregate_contributions(contributions)
    # kb_agent.py named by BOTH sources (count=2) outranks the test file (count=1),
    # and would outrank it anyway via the non-test tiebreak.
    assert aggregate.targets[0].path == "scripts/kb_agent.py"
    assert aggregate.targets[0].count == 2
    assert aggregate.targets[1].path == "scripts/test_kb_agent_safety.py"


def test_aggregate_contributions_evidence_carries_mined_at_hash() -> None:
    contributions = [
        SourceContribution(
            source_key="commit:git:sha1",
            ref="sha1abc",
            content_hash="h1",
            phrases=(MinedPhrase(phrase="release scrub pipeline", targets=("release/scrub.py",)),),
        ),
    ]
    (aggregate,) = aggregate_contributions(contributions)
    assert aggregate.evidence == (("commit:git:sha1", "sha1abc", "h1", ("release/scrub.py",)),)


def test_aggregate_contributions_is_deterministic_regardless_of_input_order() -> None:
    a = SourceContribution(
        source_key="commit:git:sha1",
        ref="sha1",
        content_hash="h1",
        phrases=(MinedPhrase(phrase="idf weight query tokens", targets=("a.py",)),),
    )
    b = SourceContribution(
        source_key="commit:git:sha2",
        ref="sha2",
        content_hash="h2",
        phrases=(MinedPhrase(phrase="idf weight query tokens", targets=("b.py",)),),
    )
    assert aggregate_contributions([a, b]) == aggregate_contributions([b, a])
