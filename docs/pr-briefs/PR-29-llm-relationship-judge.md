# PR-29 — LLM relationship judge over bounded candidates → INFERRED edges (phase 3B)

## Why

With the candidate generator proven (PR-28), the LLM judges only the **bounded, retrieved candidate
set** — never a global sweep — and emits `INFERRED_*` edges that the broker already treats as
lower-trust routing hints (PR-23). Results are cached per content+prompt+model so unchanged pairs are
never re-judged. Phase 3B of ADR-0010.

## Scope

- **`ModelClient.generate_relationship_judgment`** (extend the interface + `ChatModelClient`): given
  a candidate pair with their evidence text, return `{relation_type ∈ ontology, trust_bucket ∈
  {INFERRED_HIGH, INFERRED_LOW, AMBIGUOUS, REJECTED}, supporting_quote, reason}`. Quote-guarded: the
  `supporting_quote` must be a verbatim substring of a cited source span, else the judgment is
  downgraded to `AMBIGUOUS`/dropped (invariant 7).
- **Judge step (build):** judge candidates from the audit table; write `INFERRED_HIGH`/`INFERRED_LOW`
  results as `knowledge_edge` rows (`source=llm_judge`, evidence pointer = the quoted span);
  `AMBIGUOUS` kept out of default traversal; `REJECTED` retained in the audit table only, never as an
  edge.
- **Relationship-judgment cache (migration):** keyed by `(hash_a, hash_b, relation_schema_version,
  prompt_version, model_version)`. Cache hit ⇒ no LLM call (gates the model like
  generation/embedding caches). Idempotent on rebuild.
- **Broker:** `include_inferred=true` surfaces these labelled as non-claim-supporting routing hints
  (PR-23 already enforces the read path). Evals: inferred-edge precision + cross-domain
  evidence-recall lift vs. deterministic-only.
- Tests: judge maps candidates to ontology relations + buckets; quote-guard downgrade; cache hit
  skips the LLM (count calls); `REJECTED`/`AMBIGUOUS` never enter default traversal; rebuild idempotent.

## Do NOT

- Do not let the judge emit `EXTRACTED` (deterministic-only bucket) or `related_to`.
- Do not judge pairs that aren't candidates (no global sweep); do not bypass the cache.
- Do not let an `INFERRED_*` edge be cited as direct claim support (verifier L0 already rejects it).

## Acceptance criteria

- [ ] Judge runs only over candidates; emits ontology relations with `INFERRED_*`/`AMBIGUOUS`/
      `REJECTED` buckets; quote-guard enforced.
- [ ] Judgment cache keyed by content+schema+prompt+model; cache hit makes zero LLM calls (test).
- [ ] `INFERRED_*` edges surface only with `include_inferred=true`, labelled as routing hints;
      `AMBIGUOUS`/`REJECTED` never in default traversal.
- [ ] Inferred edges improve cross-domain evidence-recall without dropping precision below gate.
- [ ] Local run validated with Ollama (`gemma3:4b`); `make verify` + `make eval-run` green.
