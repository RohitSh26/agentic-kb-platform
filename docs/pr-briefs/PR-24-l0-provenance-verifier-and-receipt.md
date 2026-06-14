# PR-24 — L0 provenance verifier + minimal verification receipt

## Why

The broker governs retrieval, not the agent's answer; the only enforceable trust boundary is "an
answer is platform-trusted iff it carries a valid receipt" (ADR-0011). This PR ships the minimal,
deterministic, mandatory L0 verifier + receipt now (phase 1), so the trust contract exists from the
start and phase-4 levels/signing/identity add value without rework
(`docs/contracts/verification-receipt.md`).

## Scope

- **`context.verify_answer` broker tool** — request: `answer_id`, `claims[]` (each with `claim_id`,
  `text`, `evidence_ids[]`), `graph_version` (null ⇒ active), `verifier_levels` (phase 1: `["L0"]`).
  Performs the six deterministic L0 checks per cited evidence id: exists · in active version · ACL
  visible to requester · in the requester's retrieval ledger · not stale · supporting trust is
  `EXTRACTED` (not an inferred routing hint). Returns the receipt shape (minimal: `client_id` and
  `signature` null in phase 1; `verifier_levels_run=["L0"]`).
- Reject requests with no claims, or a claim with no `evidence_ids`.
- Writes a `retrieval_event` for every call (verification is a broker action). Logs ids/hashes/
  outcomes only — never answer or evidence text.
- Contract + tool schema (versioned). Update the mcp-tools contract.
- Tests: a claim citing valid, retrieved, in-version, ACL-visible `EXTRACTED` evidence ⇒ `passed`;
  each L0 failure mode flips to `failed` with the right `failed_reasons` (evidence not retrieved by
  this requester; evidence from another version; ACL-invisible; stale; supported only by an inferred
  edge); mixed claims ⇒ `partial`; `answer_hash` stable for the same normalized claims.

## Do NOT

- No L1/L2/L3 checks, no signing, no client identity (phase 4) — but keep the schema fields present
  (nullable) so phase 4 is additive.
- No generation. The verifier never produces an answer.

## Acceptance criteria

- [ ] `context.verify_answer` runs all six L0 checks and returns a spec-shaped receipt.
- [ ] Each L0 failure mode is covered by a test and produces the correct `failed_reasons`.
- [ ] Every call writes a `retrieval_event`; no answer/evidence text is logged.
- [ ] `answer_hash` is stable; `overall ∈ {passed, failed, partial}` computed correctly.
- [ ] Contract + schema versioned; `make verify` green.
