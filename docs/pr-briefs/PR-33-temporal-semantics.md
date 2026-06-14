# PR-33 — Temporal semantics (current code vs stale docs vs historical cards)

## Why

A unified graph mixes current code, possibly-stale docs, and historical cards/PRs. Without temporal
awareness the broker can answer "how does X work?" with an outdated doc, or "why was X changed?" with
only current code. The judge surfaced temporal semantics as a required gap. This PR makes retrieval
intent-aware over evidence recency/state. Phase 4; completes the trust contract.

## Scope

- **Temporal state on evidence:** each artifact carries source recency/state (current vs superseded;
  source kind: code / doc / card / PR / ADR; last-seen build/version). Derived deterministically at
  build time — no LLM.
- **Intent-aware prioritisation in the broker:** the retrieval/`intent` field (already in golden-query
  cases) weights evidence by query intent — `how_does_x_work` prioritises current code and downranks
  stale docs; `why_was_x_changed` includes cards/PRs/ADRs and historical context; `who_owns_x` favours
  ownership/recent commits. Weighting is transparent and logged, not a hidden reranker.
- **Staleness signal:** a doc that contradicts current code structure (e.g. references a removed
  symbol) is flagged stale for `how_does_x_work` intents and surfaced as a routing hint, not primary
  evidence. This is a **ranking/labelling signal only** — it never overrides the verifier's L0
  `not_stale` check, which stays a binary, deterministic provenance result. PR-33 staleness must not
  *fail* an L0 check and must not *promote* a contradicting doc into claim support; the two notions
  of "stale" are independent (L0 = source superseded/deleted in the active version; PR-33 = reranked
  for query intent).
- Evals: intent-tagged golden queries assert the right evidence ordering (current code first for
  "how", cards/PRs included for "why"); a stale doc is not returned as primary for "how".
- Tests: prioritisation per intent; stale-doc downranking; weighting logged; deterministic.

## Do NOT

- No LLM for temporal classification — derive from source state/recency deterministically.
- Do not hard-delete historical evidence — "why" questions need it; temporal weighting, not removal.

## Acceptance criteria

- [ ] Evidence carries deterministic recency/state + source kind.
- [ ] `how_does_x_work` prioritises current code and downranks stale docs; `why_was_x_changed`
      includes cards/PRs/ADRs (intent-tagged golden queries assert ordering).
- [ ] A doc referencing a removed symbol is flagged stale and not returned as primary evidence.
- [ ] Weighting is logged/transparent and deterministic; `make verify` + `make eval-run` green.
