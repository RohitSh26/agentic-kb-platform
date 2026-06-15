# Contract: Golden-query evals + evidence-recall

> Cross-cutting contract for `evals/`. Defines the golden-query case shape and the metrics that gate
> a publish. Builds on the existing harness (`retrieval_cases`, `missing_context_rate`).

## Why

The judge's headline risk for Design A is **underlinking**: the system returns *real* citations and
looks successful while silently missing the one ADR/card/symbol that actually answers the question.
Happy-path retrieval tests do not catch this. The defence is a **golden-query set with expected
evidence**, scored by **evidence-recall**, used as a **publish gate** (`publish-gates.md`).

## Golden-query case shape

A case lives under `evals/retrieval_cases/` (golden subset) as structured data:

```json
{
  "case_id": "code-structure/where-is-x-defined-001",
  "query": "Where is the retrieval ledger written?",
  "intent": "how_does_x_work",            // drives temporal weighting (current code prioritised)
  "requester_teams": ["platform"],         // ACL context the query runs under
  "expected_evidence_ids": ["ev_…", "ev_…"],   // the evidence that MUST appear (recall numerator)
  "expected_edge_types": ["calls", "imports"], // optional: relations that must be discoverable
  "must_not_leak_ids": ["ev_restricted_…"],    // optional: ACL negative — must NEVER appear
  "min_evidence_recall": 1.0               // per-case threshold (default from the gate)
}
```

`intent ∈ { how_does_x_work, why_was_x_changed, who_owns_x, what_calls_x }` (extensible). Used in
phase 4 temporal weighting; recorded from phase 0 so cases are stable.

## Metrics

- **evidence_recall** = |returned ∩ expected_evidence_ids| / |expected_evidence_ids|, averaged over
  the golden set. First-class metric; generalises the existing `missing_context_rate`
  (`evidence_recall ≈ 1 − missing_context_rate` on the golden subset).
- **edge_precision / edge_recall** (per `edge_type`): of edges the system surfaces for a case, how
  many are correct (precision) and how many of the expected relations were found (recall). Reported
  per relation type so a weak relation can't hide behind a strong one.
- **acl_leak_count** = number of `must_not_leak_ids` that appeared. MUST be 0.
- **intent_ordering_ok** (PR-33) = did the broker order returned evidence as the case's
  `intent` requires? `None` when the case asserts no ordering (recall-only cases are
  unaffected); else `True` iff the **primary** (first) returned `source_kind` is a lead kind
  for the intent (current code for `how_does_x_work` / `what_calls_x`; a card/PR/ADR for
  `why_was_x_changed`), at least one history kind is present for `why`, and **no PR-33-stale
  doc is primary**. `aggregate` reports `intent_ordering_failures` (offending `case_id`s).
- **token cost** per case (already tracked) — guards against buying recall with budget blowout.

## Gate thresholds (see publish-gates.md for the authoritative numbers)

- `evidence_recall >= 0.95` on the golden set (per-case floor `min_evidence_recall`).
- `acl_leak_count == 0` (hard).
- No `edge_type` with `edge_precision < 0.9` once that relation is in production.
- No regression > the harness's existing regression margin vs the recorded baseline.

## Phasing

- Phase 0: this contract + a skeleton golden set + the evidence-recall metric wired into the harness
  (can run, may be near-empty).
- Phase 1: first real golden queries for code-structure (symbols/imports/calls) — doubles as the
  graphify acceptance test.
- Phase 2+: cross-domain golden queries; relation precision/recall as enforcing gates.
- Phase 4 (PR-33): `intent`-tagged ordering metric (`intent_ordering_ok`) over the broker's
  deterministic temporal weighting — current code first for `how`, cards/PRs/ADRs included for
  `why`, no stale doc primary for `how`.
