"""Deterministic alias-phrase extraction + aggregation (PR-38, zero LLM).

Pure string functions over content that already lives in Postgres rows: commit
subjects + changed-file lists (`commit` artifacts persist both in body_text) and
markdown filename slugs (brief/ADR titles are slugified filenames in this repo).
Same input ⇒ same phrases ⇒ same alias rows, so the build-time pass in
`alias/run.py` is idempotent by construction. Rules and the body-JSON contract:
docs/contracts/alias-reference.md.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Conventional-commit types whose leading token is a TYPE (the parenthesized
# scope, if any, is the alias-worthy token). Any other leading `label:` token
# (e.g. "docify:", "broker:") is itself treated as the scope.
CONVENTIONAL_TYPES = frozenset(
    {"build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"}
)

# Breaks n-gram runs and is dropped from normalized phrases/queries. Deliberately
# small and generic — a stopword here can never appear inside an alias phrase.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "nor",
        "not",
        "no",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "done",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "by",
        "as",
        "into",
        "onto",
        "over",
        "under",
        "after",
        "before",
        "against",
        "between",
        "during",
        "without",
        "within",
        "about",
        "above",
        "below",
        "out",
        "off",
        "up",
        "down",
        "again",
        "further",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "so",
        "such",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "own",
        "same",
        "too",
        "very",
        "than",
        "then",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "how",
        "what",
        "while",
        "can",
        "could",
        "should",
        "would",
        "may",
        "might",
        "must",
        "will",
        "shall",
        "have",
        "has",
        "had",
        "having",
        "we",
        "our",
        "ours",
        "you",
        "your",
        "yours",
        "i",
        "he",
        "she",
        "they",
        "them",
        "their",
        "his",
        "her",
        "also",
        "just",
        "only",
        "ever",
        "never",
        "per",
        "via",
    ]
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SUBJECT_LABEL = re.compile(
    r"^\s*(?P<lead>[A-Za-z][\w./-]*)(?:\((?P<scope>[^)]*)\))?!?:\s*(?P<desc>.+)$"
)

NGRAM_MIN = 2
NGRAM_MAX = 4
# A slug phrase needs >= 2 meaningful tokens ("overview" alone is not an alias).
MIN_SLUG_TOKENS = 2
# Ranked-target cap in the body JSON: bounded rows, and a phrase that names more
# than this many files is a routing signal too diffuse to enumerate further.
MAX_BODY_TARGETS = 50


def tokenize(text: str) -> list[str]:
    """Lowercase alnum tokens, length >= 2. Length-1 fragments (the "s" of
    "source's", a lone "0") are dropped WITHOUT breaking n-gram runs — they are
    intra-word artifacts of punctuation stripping, not real words."""
    return [t for t in _NON_ALNUM.split(text.lower()) if len(t) >= 2]


def normalize_phrase(text: str) -> str:
    """The canonical alias/query form: tokenized, stopwords removed, space-joined.
    Applied identically to mined phrases and resolver queries."""
    return " ".join(t for t in tokenize(text) if t not in STOPWORDS)


def phrase_variants(phrase: str) -> tuple[str, ...]:
    """Deterministic search_text variants so keyword queries like `kb_search` or
    `kb-search` still match the alias row. Pure function of the phrase — never
    stored state."""
    tokens = phrase.split()
    if len(tokens) < 2:
        return ()
    return ("-".join(tokens), "_".join(tokens))


def parse_subject_label(subject: str) -> tuple[str | None, str]:
    """Split a commit subject into (scope_text, description).

    `feat(kb-builder): X` -> ("kb-builder", "X"); a non-conventional leading
    label `docify: X` -> ("docify", "X"); no label -> (None, subject).
    """
    match = _SUBJECT_LABEL.match(subject)
    if match is None:
        return None, subject.strip()
    lead = match.group("lead")
    scope = match.group("scope")
    description = match.group("desc").strip()
    if lead.lower() in CONVENTIONAL_TYPES:
        return (scope if scope else None), description
    # Non-conventional label: the label itself is the scope; a parenthesized
    # qualifier (rare) narrows it further and wins.
    return (scope if scope else lead), description


def ngram_phrases(description: str) -> set[str]:
    """Stopword-filtered 2-4-word n-grams over CONTIGUOUS non-stopword token runs.

    Stopwords break the window (an n-gram never spans "the"/"is"/...), so the
    mined phrases stay close to what a developer would actually type.
    """
    runs: list[list[str]] = [[]]
    for token in tokenize(description):
        if token in STOPWORDS:
            if runs[-1]:
                runs.append([])
            continue
        runs[-1].append(token)
    phrases: set[str] = set()
    for run in runs:
        for size in range(NGRAM_MIN, NGRAM_MAX + 1):
            for start in range(len(run) - size + 1):
                phrases.add(" ".join(run[start : start + size]))
    return phrases


def doc_slug_phrase(path: str) -> str | None:
    """Filename slug of a markdown doc, or None.

    `docs/pr-briefs/PR-38-alias-reference-index.md` -> "alias reference index";
    `docs/adr/0030-twelve-role-roster-and-langgraph-backend.md` ->
    "twelve role roster langgraph backend" (stopwords removed, same as queries).
    Leading PR/ADR/date/number tokens are stripped; needs >= MIN_SLUG_TOKENS left.
    """
    filename = path.rsplit("/", 1)[-1]
    if not filename.lower().endswith(".md"):
        return None
    stem_tokens = tokenize(filename[: -len(".md")])
    index = 0
    while index < len(stem_tokens) and (
        stem_tokens[index].isdigit() or stem_tokens[index] in ("pr", "adr")
    ):
        index += 1
    phrase = " ".join(t for t in stem_tokens[index:] if t not in STOPWORDS)
    if len(phrase.split()) < MIN_SLUG_TOKENS:
        return None
    return phrase


@dataclass(frozen=True)
class MinedPhrase:
    """One normalized phrase a source contributes, with its per-source targets."""

    phrase: str
    targets: tuple[str, ...]  # sorted paths


def _merge(phrases: dict[str, set[str]], phrase: str, targets: Iterable[str]) -> None:
    if not phrase:
        return
    phrases.setdefault(phrase, set()).update(targets)


def mine_commit(subject: str, changed_files: Sequence[str]) -> tuple[MinedPhrase, ...]:
    """Mine one commit: scope tokens + subject n-grams (targets = the commit's
    changed files) and, for each changed docs/**/*.md, its filename slug
    (target = that file only). Deterministic; returns phrases sorted."""
    files = tuple(sorted(set(changed_files)))
    phrases: dict[str, set[str]] = {}
    scope_text, description = parse_subject_label(subject)
    if files:
        if scope_text is not None:
            _merge(phrases, normalize_phrase(scope_text), files)
        for gram in ngram_phrases(description):
            _merge(phrases, gram, files)
    for path in files:
        if not path.startswith("docs/"):
            continue
        slug = doc_slug_phrase(path)
        if slug is not None:
            _merge(phrases, slug, (path,))
    return tuple(
        MinedPhrase(phrase=p, targets=tuple(sorted(t))) for p, t in sorted(phrases.items())
    )


def mine_doc_source(path: str) -> tuple[MinedPhrase, ...]:
    """Mine one ingested markdown doc source: its filename slug targets itself.
    Covers production KBs where briefs/ADRs are ingested as github_doc sources."""
    slug = doc_slug_phrase(path)
    if slug is None:
        return ()
    return (MinedPhrase(phrase=slug, targets=(path,)),)


@dataclass(frozen=True)
class SourceContribution:
    """One source's mined output + the skip watermark (content_hash mined at)."""

    source_key: str  # "commit:<source_uri>" | "doc:<source_uri>"
    ref: str  # short evidence ref (sha12 / doc path)
    content_hash: str
    phrases: tuple[MinedPhrase, ...]


def is_test_path(path: str) -> bool:
    filename = path.rsplit("/", 1)[-1]
    return "/tests/" in f"/{path}" or filename.startswith("test_")


def _name_overlap(phrase: str, path: str) -> int:
    filename_tokens = set(tokenize(path.rsplit("/", 1)[-1]))
    return len(filename_tokens & set(phrase.split()))


@dataclass(frozen=True)
class RankedTarget:
    path: str
    count: int  # number of distinct contributing sources naming this path


@dataclass(frozen=True)
class AliasAggregate:
    """One desired alias row: phrase + ranked targets + per-source evidence."""

    phrase: str
    confirmation_count: int
    targets: tuple[RankedTarget, ...]
    # (source_key, ref, content_hash, targets-of-this-phrase-from-that-source)
    evidence: tuple[tuple[str, str, str, tuple[str, ...]], ...]


def aggregate_contributions(
    contributions: Sequence[SourceContribution],
) -> tuple[AliasAggregate, ...]:
    """Fold per-source mining into per-phrase aggregates (pure, deterministic).

    The same normalized phrase seen in N sources gets confirmation_count=N and
    the union of targets ranked by: count desc, non-test before test, filename-
    token overlap with the phrase desc, path asc. The rank makes top-1 resolution
    meaningful when a single commit touched several files.
    """
    per_phrase: dict[str, list[tuple[SourceContribution, tuple[str, ...]]]] = {}
    for contribution in contributions:
        for mined in contribution.phrases:
            per_phrase.setdefault(mined.phrase, []).append((contribution, mined.targets))
    aggregates: list[AliasAggregate] = []
    for phrase, entries in sorted(per_phrase.items()):
        counts: dict[str, int] = {}
        for _, targets in entries:
            for path in targets:
                counts[path] = counts.get(path, 0) + 1
        ranked = sorted(
            counts.items(),
            key=lambda item: (
                -item[1],
                is_test_path(item[0]),
                -_name_overlap(phrase, item[0]),
                item[0],
            ),
        )[:MAX_BODY_TARGETS]
        evidence = tuple(
            sorted((c.source_key, c.ref, c.content_hash, targets) for c, targets in entries)
        )
        aggregates.append(
            AliasAggregate(
                phrase=phrase,
                confirmation_count=len(entries),
                targets=tuple(RankedTarget(path=p, count=n) for p, n in ranked),
                evidence=evidence,
            )
        )
    return tuple(aggregates)


__all__ = [
    "MAX_BODY_TARGETS",
    "STOPWORDS",
    "AliasAggregate",
    "MinedPhrase",
    "RankedTarget",
    "SourceContribution",
    "aggregate_contributions",
    "doc_slug_phrase",
    "is_test_path",
    "mine_commit",
    "mine_doc_source",
    "ngram_phrases",
    "normalize_phrase",
    "parse_subject_label",
    "phrase_variants",
    "tokenize",
]
