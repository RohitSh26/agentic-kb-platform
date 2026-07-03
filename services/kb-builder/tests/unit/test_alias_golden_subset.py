"""Hermetic 5-case subset of the alias golden set (PR-38 brief: "Hermetic pytest
covers a seeded 5-case subset"; full 25-case run: scripts/eval_alias_resolution.py
+ evals/retrieval_cases/alias_golden_v1.yaml).

Exercises the REAL pipeline — mine_commit -> aggregate_contributions -> resolve —
end to end, pure and DB-free: each commit's subject + changed-file list is SEEDED
here verbatim from this repo's actual git history (`git show -s --format=%s <sha>`
/ `git show --name-only <sha>`, recorded below), not fetched — matching how
alias/run.py itself computes them from a live `commit` artifact's body_text. This
proves the mining + aggregation + resolution CODE, independent of Postgres.

The 5 cases are alias-01, alias-10, alias-16, alias-20, alias-25 from
evals/retrieval_cases/alias_golden_v1.yaml, chosen to cover: a doc-filename-slug
alias, multi-source confirmation (two commits touching one ADR), a fuzzy match
where mining fragments span a stopword, and an exact n-gram match.
"""

from agentic_kb_builder.alias.mining import SourceContribution, aggregate_contributions, mine_commit
from agentic_kb_builder.alias.resolve import AliasEntry, resolve


def _entries_from_commits(commits: list[tuple[str, tuple[str, ...]]]) -> list[AliasEntry]:
    """commits: [(subject, changed_files), ...] -> the resolver's AliasEntry list,
    exactly the shape alias/run.py builds from live commit artifacts + evidence."""
    contributions = [mine_commit(subject, files) for subject, files in commits]
    seeded = [
        SourceContribution(
            source_key=f"commit:git:sha{i}",
            ref=f"sha{i}",
            content_hash=f"h{i}",
            phrases=phrases,
        )
        for i, phrases in enumerate(contributions)
    ]
    aggregates = aggregate_contributions(seeded)
    return [
        AliasEntry(
            alias=agg.phrase,
            targets=tuple(t.path for t in agg.targets),
            confirmation_count=agg.confirmation_count,
        )
        for agg in aggregates
    ]


# --- seeded commits (git show -s --format=%s <sha> / git show --name-only <sha>) ---

_E1433E3 = (
    "docs: autonomous execution plan (acceptance criteria + eval system) and PR-38/39/40 briefs",
    (
        "docs/pr-briefs/PR-38-alias-reference-index.md",
        "docs/pr-briefs/PR-39-get-task-context-langgraph.md",
        "docs/pr-briefs/PR-40-review-panel-service.md",
        "docs/proposals/2026-07-02-autonomous-execution-plan.md",
    ),
)

_129D3E1 = (
    "fix(kb-builder): durable model-output cache is fail-soft, never crashes the build",
    (
        "services/kb-builder/src/agentic_kb_builder/infrastructure/postgres/durable_output_cache.py",
        "services/kb-builder/tests/unit/test_durable_cache_failsoft.py",
    ),
)

_420AF45 = (
    "Smoke client exercises context.expand against the connected graph",
    ("scripts/smoke_client.py",),
)

_05A541B = (
    "fix(kb-builder): one source's failure no longer aborts the whole build",
    (
        "services/kb-builder/src/agentic_kb_builder/application/build_runner.py",
        "services/kb-builder/tests/integration/test_build_engine.py",
    ),
)

_3AF8E5B = (
    "ADR-0021: enforced human-approval gate at every agent delegation",
    ("docs/adr/0021-human-approval-delegation-gate.md",),
)

_EAD115A = (
    "ADR-0021: orchestrator routing + plan-of-action gate (gate count = delegations)",
    ("docs/adr/0021-human-approval-delegation-gate.md",),
)


def test_alias_01_doc_filename_slug_inside_a_commit_resolves_exactly() -> None:
    entries = _entries_from_commits([_E1433E3])
    result = resolve("the alias reference index", entries)
    assert result is not None
    assert result.matched == "exact"
    assert result.targets[0] == "docs/pr-briefs/PR-38-alias-reference-index.md"


def test_alias_10_multi_source_confirmation_via_doc_slug_and_ngrams() -> None:
    entries = _entries_from_commits([_3AF8E5B, _EAD115A])
    # both commits contribute the SAME scope phrase ("adr 0021"), so the miner's
    # cross-source aggregation must confirm it from 2 distinct sources.
    scope = next(e for e in entries if e.alias == "adr 0021")
    assert scope.confirmation_count == 2
    # both commits ALSO changed the ADR itself, so its own filename slug
    # ("human approval delegation gate", bullet 1's doc-slug rule) is mined from
    # commit 1's changed-file list — an EXACT match, not a fuzzy guess.
    result = resolve("human approval delegation gate", entries)
    assert result is not None
    assert result.matched == "exact"
    assert result.targets[0] == "docs/adr/0021-human-approval-delegation-gate.md"


def test_alias_16_fuzzy_resolution_ranks_the_fix_over_its_test() -> None:
    entries = _entries_from_commits([_129D3E1])
    result = resolve("the durable cache fail soft fix", entries)
    assert result is not None
    assert result.matched == "fuzzy"
    assert result.targets[0] == (
        "services/kb-builder/src/agentic_kb_builder/infrastructure/postgres/durable_output_cache.py"
    )


def test_alias_20_fuzzy_resolution_across_a_stopword_gap() -> None:
    entries = _entries_from_commits([_05A541B])
    result = resolve("source failure aborts the build", entries)
    assert result is not None
    assert result.matched == "fuzzy"
    assert result.targets[0] == (
        "services/kb-builder/src/agentic_kb_builder/application/build_runner.py"
    )


def test_alias_25_unlabeled_subject_ngram_resolves_exactly() -> None:
    entries = _entries_from_commits([_420AF45])
    result = resolve("the smoke client", entries)
    assert result is not None
    assert result.matched == "exact"
    assert result.targets == ("scripts/smoke_client.py",)


def test_seeded_five_case_subset_top1_accuracy_is_100_percent() -> None:
    """The brief's target is >= 80% top-1 over the full 25; this hermetic subset
    (chosen to cover exact + fuzzy + multi-source paths) is a 100% floor check
    that the mining+resolve CODE has no regressions — a broader miss budget is
    for mining-coverage gaps in the full 25-case run, not code correctness."""
    cases = [
        ([_E1433E3], "the alias reference index", "docs/pr-briefs/PR-38-alias-reference-index.md"),
        (
            [_3AF8E5B, _EAD115A],
            "human approval delegation gate",
            "docs/adr/0021-human-approval-delegation-gate.md",
        ),
        (
            [_129D3E1],
            "the durable cache fail soft fix",
            "services/kb-builder/src/agentic_kb_builder/infrastructure/postgres/durable_output_cache.py",
        ),
        (
            [_05A541B],
            "source failure aborts the build",
            "services/kb-builder/src/agentic_kb_builder/application/build_runner.py",
        ),
        ([_420AF45], "the smoke client", "scripts/smoke_client.py"),
    ]
    hits = 0
    for commits, query, expected_top1 in cases:
        entries = _entries_from_commits(commits)
        result = resolve(query, entries)
        if result is not None and result.targets and result.targets[0] == expected_top1:
            hits += 1
    assert hits == len(cases)
