# PR-31 — Verifier L3 (LLM entailment, cached) + signed verification receipts

## Why

Some claims can't be checked deterministically (paraphrase, synthesis across evidence). L3 handles
exactly those — and only those — with a cached LLM entailment check. Signing turns the receipt into a
verifiable artifact a host can trust. Together they complete the verifier and make "platform-trusted"
cryptographically checkable (ADR-0011, `verification-receipt.md`). Phase 4.

## Scope

- **Verifier L3 — LLM entailment (cached):** only for claims L0–L2 could not adjudicate. Check
  whether the cited evidence entails the claim; return pass/fail + reason into `checks.L3_entailment`.
  Cache keyed by `(claim_hash, evidence_ids_hash, prompt_version, model_version)` so re-verification
  of an unchanged claim makes no LLM call. Runs locally on Ollama (`gemma3:4b`) for tests/dev.
- **Signed receipts:** sign the receipt over `answer_hash` + `graph_version` + `claim_results` with a
  configured key (key referenced by env/Key Vault name only — never a literal). Add `signature` +
  key id. Provide a stateless verify path so a host can validate a receipt without re-running checks.
- `verifier_levels` may include `L3`; policy decides when L3 runs (cost guard). Receipt schema
  already reserves `signature` — this populates it; no schema break.
- Tests: an entailed claim ⇒ L3 pass, a non-entailed paraphrase ⇒ L3 fail; L3 cache hit makes zero
  LLM calls; signature validates and a tampered `answer_hash`/`claim_results` fails validation; no
  secret material in logs/fixtures.

## Do NOT

- Do not run L3 on claims L2 already resolved (cost discipline). Do not embed any key material in
  code/fixtures/logs.
- Do not require L3 for a receipt to exist — L0 stays the mandatory floor.

## Acceptance criteria

- [ ] L3 entailment runs only on deterministically-unresolved claims; cached by claim+evidence+model.
- [ ] Receipts are signed; a host can validate one statelessly; tampering fails validation.
- [ ] Cache hit ⇒ zero LLM calls (test); local validation on Ollama.
- [ ] No secret value in code, fixtures, or logs.
- [ ] `make verify` green.
