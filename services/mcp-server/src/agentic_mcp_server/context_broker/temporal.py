"""Deterministic temporal semantics for evidence (PR-33, ADR-0010/0011 phase 4).

A unified graph mixes CURRENT code, possibly-stale docs, and historical
cards/PRs/ADRs. Without temporal awareness the broker can answer "how does X
work?" with an outdated doc, or "why was X changed?" with only current code.
This module derives, with NO LLM, a recency/state and a source KIND for each
artifact, then computes a TRANSPARENT, LOGGED, deterministic ranking weight per
query intent (docs/contracts/golden-query-evals.md `intent`).

Two notions of "stale" are deliberately INDEPENDENT and must stay so:

  * L0 `not_stale` (context_broker/verify.py) — a binary provenance check: the
    cited source is superseded/deleted in the ACTIVE version. PR-33 NEVER touches
    it: a doc this module downranks for a `how` query still PASSES L0 as long as
    its source is in-version.
  * PR-33 staleness — a RANKING/LABELLING signal: a doc that contradicts the
    current code structure (references a removed/absent symbol) is downranked and
    flagged for `how_does_x_work` intents and surfaced as a routing hint, NOT as
    primary evidence. It can never promote a contradicting doc into claim support
    and never fails an L0 check.

Everything here is pure and deterministic: same inputs ⇒ same kind/state/weight
⇒ same ranking order (the ranker uses a stable artifact_id tie-break).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from agentic_mcp_server.structured_logging import get_logger

logger = get_logger(__name__)

# The query intents the golden set tags (golden-query-evals.md). Mirrors
# evals/harness/golden.py GoldenIntent — duplicated, not imported, across the
# service boundary (ADR-0008). None ⇒ no intent supplied ⇒ neutral weighting.
Intent = Literal[
    "how_does_x_work",
    "why_was_x_changed",
    "who_owns_x",
    "what_calls_x",
]

# Deterministic source kinds derived from (source_type, artifact_type). `other`
# is the safe default so an unrecognised pair never silently mis-weights.
SourceKind = Literal["code", "doc", "card", "pr", "adr", "other"]

# `current` = a live, in-version artifact; `superseded` = retained for history
# ("why" needs it) but no longer the live revision of its identity.
TemporalState = Literal["current", "superseded"]


@dataclass(frozen=True)
class TemporalSignals:
    """The minimal, already-stored fields temporal derivation reads.

    All four are columns the broker already hydrates (or can hydrate without an
    LLM): no new generation, no schema change. `invalidated_at_seq`/`source_is_
    deleted` come from the version-membership interval (version-membership.md);
    `source_type`/`artifact_type` are the connector + builder kinds.
    """

    source_type: str | None
    artifact_type: str
    invalidated_at_seq: int | None
    source_is_deleted: bool


def derive_source_kind(source_type: str | None, artifact_type: str) -> SourceKind:
    """Map (source_type, artifact_type) to a coarse temporal source kind.

    Deterministic and total: an unknown pair falls through to `other`. Code is
    keyed off the builder's structural artifact_types (graphify emits these),
    everything else off the connector source_type so a doc that happens to
    mention code is still a doc.
    """
    if artifact_type in _CODE_ARTIFACT_TYPES:
        return "code"
    st = (source_type or "").lower()
    at = artifact_type.lower()
    if st in _CODE_SOURCE_TYPES:
        return "code"
    if at == "adr" or at == "decision":
        return "adr"
    if st == "ado_card" or at == "card" or at == "work_item":
        return "card"
    if at in _PR_ARTIFACT_TYPES or st == "git_metadata":
        # git_metadata sources are commit/PR history rows (the "why" trail).
        return "pr"
    if st in _DOC_SOURCE_TYPES or at in _DOC_ARTIFACT_TYPES:
        return "doc"
    return "other"


# Structural code artifact_types graphify emits (kb-builder graphify backend).
_CODE_ARTIFACT_TYPES = frozenset({"code_file", "code_symbol", "endpoint", "test"})
_CODE_SOURCE_TYPES = frozenset({"github_code"})
_PR_ARTIFACT_TYPES = frozenset({"commit", "pull_request", "pr"})
_DOC_SOURCE_TYPES = frozenset({"github_doc", "azure_wiki"})
_DOC_ARTIFACT_TYPES = frozenset({"doc_chunk", "chunk", "summary", "concept", "doc"})


def derive_state(signals: TemporalSignals) -> TemporalState:
    """`superseded` iff the artifact's source is deleted OR the artifact left the
    KB at some build (invalidated_at_seq set). Otherwise `current`.

    Note: the broker only ever hydrates artifacts that are MEMBERS of the active
    build_seq (fetch_artifacts interval predicate), so a `superseded` artifact
    here is one retained for history that is still being served — never a row
    from a foreign version. This is independent of L0 `not_stale`.
    """
    if signals.source_is_deleted or signals.invalidated_at_seq is not None:
        return "superseded"
    return "current"


# Per-intent kind weights. Multiplicative on the rank score; > 1 lifts, < 1
# downranks. Numbers are intentionally coarse and TRANSPARENT (no hidden
# reranker) — they only reorder within the already-ACL-filtered candidate set.
_INTENT_KIND_WEIGHT: dict[Intent, dict[SourceKind, float]] = {
    # "how does X work" wants CURRENT code first; docs/cards are context.
    "how_does_x_work": {
        "code": 1.5,
        "doc": 1.0,
        "adr": 0.9,
        "pr": 0.8,
        "card": 0.7,
        "other": 1.0,
    },
    # "why was X changed" wants the change trail: cards/PRs/ADRs lifted, code is
    # still relevant but not the headline.
    "why_was_x_changed": {
        "card": 1.5,
        "pr": 1.5,
        "adr": 1.4,
        "doc": 1.0,
        "code": 0.9,
        "other": 1.0,
    },
    # "who owns X" favours ownership/recent-commit signal (PRs/commits, cards).
    "who_owns_x": {
        "pr": 1.5,
        "card": 1.3,
        "code": 1.1,
        "adr": 1.0,
        "doc": 0.9,
        "other": 1.0,
    },
    # "what calls X" is a code-structure question: code first.
    "what_calls_x": {
        "code": 1.4,
        "doc": 0.9,
        "adr": 0.9,
        "pr": 0.9,
        "card": 0.8,
        "other": 1.0,
    },
}

# State multiplier: a superseded artifact is downranked but NEVER removed
# (the "why" trail needs history — weight, don't delete). Tunable; coarse.
_SUPERSEDED_WEIGHT = 0.6
# A doc flagged PR-33-stale for a `how` intent is pushed below any primary
# evidence so it can only ever be a routing hint, not the headline.
_STALE_DOC_WEIGHT = 0.25


@dataclass(frozen=True)
class TemporalWeight:
    """The transparent breakdown of one artifact's temporal weight for an intent.

    Carried so the ranker can log every factor (event=temporal_weight_*) — the
    weighting is auditable, not a black box.
    """

    source_kind: SourceKind
    state: TemporalState
    stale_for_intent: bool
    weight: float


def compute_weight(
    *,
    artifact_id: uuid.UUID,
    intent: Intent | None,
    signals: TemporalSignals,
    stale_for_intent: bool,
) -> TemporalWeight:
    """Deterministic temporal multiplier for one artifact under `intent`.

    Logged at INFO as `event=temporal_weight` with every factor so the reorder is
    transparent (no hidden reranker). With `intent=None` the kind weight is 1.0
    (neutral) but state/staleness still apply — those are intent-independent
    quality signals.
    """
    kind = derive_source_kind(signals.source_type, signals.artifact_type)
    state = derive_state(signals)

    kind_weight = 1.0
    if intent is not None:
        kind_weight = _INTENT_KIND_WEIGHT[intent].get(kind, 1.0)

    state_weight = _SUPERSEDED_WEIGHT if state == "superseded" else 1.0
    stale_weight = _STALE_DOC_WEIGHT if stale_for_intent else 1.0
    weight = kind_weight * state_weight * stale_weight

    logger.info(
        "event=temporal_weight artifact_id=%s intent=%s kind=%s state=%s "
        "stale_for_intent=%s kind_weight=%.3f state_weight=%.3f stale_weight=%.3f weight=%.3f",
        artifact_id,
        intent or "none",
        kind,
        state,
        stale_for_intent,
        kind_weight,
        state_weight,
        stale_weight,
        weight,
    )
    return TemporalWeight(
        source_kind=kind,
        state=state,
        stale_for_intent=stale_for_intent,
        weight=weight,
    )


# Intents for which a doc contradicting current code structure is a stale,
# downranked routing hint. "why_was_x_changed" deliberately NOT here: a doc that
# references a now-removed symbol is exactly the historical context "why" wants.
_STALENESS_INTENTS: frozenset[Intent] = frozenset({"how_does_x_work", "what_calls_x"})

# A code symbol reference inside a doc: a backtick-quoted identifier (the
# convention docs use for code), e.g. `helper`, `pkg.util.helper`,
# `EvidenceCard`. Deterministic, regex-only — no LLM. Dotted/qualified names are
# split so a doc citing `pkg.util.helper` matches a `helper` symbol title.
_SYMBOL_REFERENCE_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
# An identifier that looks like a code symbol (has a lower/upper letter, not a
# pure word a doc would use prose-style). We only ever FLAG when a referenced
# symbol is absent from the CURRENT set, so prose false-positives cannot mark a
# doc stale unless they are backtick-quoted AND absent from current code.
_MIN_SYMBOL_LEN = 2


def referenced_symbols(text_value: str | None) -> frozenset[str]:
    """Backtick-quoted code symbols a doc references (deterministic, regex-only).

    Each match contributes both its full dotted form and its trailing segment, so
    a doc citing `pkg.util.helper` matches a symbol whose title is `helper`.
    """
    if not text_value:
        return frozenset()
    found: set[str] = set()
    for match in _SYMBOL_REFERENCE_RE.findall(text_value):
        if len(match) < _MIN_SYMBOL_LEN:
            continue
        found.add(match)
        if "." in match:
            tail = match.rsplit(".", 1)[-1]
            if len(tail) >= _MIN_SYMBOL_LEN:
                found.add(tail)
    return frozenset(found)


def is_stale_doc_for_intent(
    *,
    intent: Intent | None,
    source_kind: SourceKind,
    body_text: str | None,
    title: str | None,
    current_symbols: frozenset[str],
) -> bool:
    """True iff a DOC references a code symbol absent from the current code set
    under a structure-seeking intent.

    A pure ranking/labelling signal: it never fails an L0 check and never promotes
    a contradicting doc into claim support — it only downranks the doc so it can be
    a routing hint, not primary evidence. Returns False for non-doc kinds, for
    intents that want history, and for docs that reference only current symbols (or
    no symbols at all).
    """
    if intent not in _STALENESS_INTENTS or source_kind != "doc":
        return False
    refs = referenced_symbols(body_text) | referenced_symbols(title)
    if not refs:
        return False
    # Stale iff at least one referenced symbol is NOT a current code member, AND
    # the doc references at least one code-symbol-shaped name (refs non-empty).
    return any(ref not in current_symbols for ref in refs)
