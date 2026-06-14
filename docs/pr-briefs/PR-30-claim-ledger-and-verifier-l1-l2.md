# PR-30 — Claim/evidence ledger + verifier L1 (coverage) and L2 (typed-fact)

## Why

L0 (PR-24) proves an answer cites real, retrieved, in-version, ACL-visible `EXTRACTED` evidence — but
not that the evidence *supports* the claim. The judge's "biggest hole" is an end-to-end trust
contract. This PR adds the typed claim/evidence ledger and the deterministic L1/L2 verifier levels
on top of the existing receipt, no schema break (`verification-receipt.md`). Phase 4.

## Scope

- **Claim/evidence ledger:** typed, ID-stable citeable units over existing data — AST facts (symbol/
  import/call with span), prose facts (statement + source span), edge facts (a relation + its
  evidence). A view/table layer (no new truth store; reads from `knowledge_artifact`/`knowledge_edge`
  + spans). Each unit exposes the typed assertion the verifier can check.
- **Verifier L1 — citation coverage + span caps:** every claim cites ≥1 evidence unit; quoted spans
  are within configured caps; flag claims with no citation. Adds `L1_coverage` to `checks`.
- **Verifier L2 — typed-fact checks (no LLM):** for fact types the ledger can adjudicate
  deterministically (e.g. "symbol X is defined in file F", "F imports M", "edge of type T exists
  between A and B"), check the claim against the typed unit. Adds `L2_typed_fact` to `checks`.
- `context.verify_answer` accepts `verifier_levels` up to `["L0","L1","L2"]`; receipt
  `verifier_levels_run` reflects what ran. All additive — phase-1 receipts stay valid.
- Tests: a claim whose typed fact matches the ledger ⇒ L2 pass; a claim that misreads the evidence
  (quote present but assertion false) ⇒ L2 fail (the case invariant 7 alone misses); uncited claim ⇒
  L1 fail; span over cap ⇒ L1 fail.

## Do NOT

- No LLM entailment yet (L3 is PR-31). No signing / client identity yet (PR-31 / PR-32).
- Do not duplicate truth — the ledger is a typed read layer over existing tables.

## Acceptance criteria

- [ ] Ledger exposes typed AST/prose/edge facts with stable ids + spans.
- [ ] L1 flags uncited claims and span-cap violations; L2 deterministically checks typed facts.
- [ ] A quote-present-but-misread claim fails L2 (covered by a test).
- [ ] Receipt remains backward-compatible; `verifier_levels_run` accurate.
- [ ] `make verify` green.
