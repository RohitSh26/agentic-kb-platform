# Contract: Verification receipt

> Cross-service contract. Produced by the mcp-server verifier tool; consumed by hosts/clients that
> choose to enforce "platform-trusted ⇒ valid receipt". Versioned: `receipt_schema_version = 1`.

## Why this exists

The broker governs retrieval, not the agent's final answer. The only enforceable trust boundary
against agents we don't control is: **an answer is platform-trusted iff it carries a valid receipt.**
The receipt is the verifier's signed statement about which claims were checked and how.

## The verifier tool

`context.verify_answer` (request/response below). It runs the layered verifier from ADR-0011 (L0
mandatory in phase 1; L1–L3 added in phase 4) and returns a receipt. It performs **no generation**.

### Request

```json
{
  "answer_id": "string",                         // host-assigned id for the answer being verified
  "claims": [                                     // 1..N claims the agent asserts
    { "claim_id": "c1", "text": "…", "evidence_ids": ["ev_…", "ev_…"] }
  ],
  "graph_version": "string|null",                // null ⇒ active version
  "verifier_levels": ["L0"]                       // requested levels; server may run fewer/more per policy
}
```

Reject a request with no `claims`, or any claim with no `evidence_ids` (that claim fails L1 by
definition; in phase 1 it fails L0 provenance because there is nothing to check).

### Response (the receipt)

```json
{
  "receipt_schema_version": 1,
  "answer_hash": "sha256 of normalized answer claims",
  "graph_version": "string",
  "issued_at": "timestamptz",
  "verifier_levels_run": ["L0"],
  "overall": "passed | failed | partial",
  "claim_results": [
    {
      "claim_id": "c1",
      "result": "passed | failed",
      "checks": {
        "L0_exists": true,
        "L0_in_active_version": true,
        "L0_acl_visible": true,
        "L0_in_requester_ledger": true,
        "L0_not_stale": true,
        "L0_supporting_trust_ok": true        // cited support is EXTRACTED, not an INFERRED hint
      },
      "failed_reasons": []
    }
  ],
  "client_id": "string|null",                   // reserved (phase 4 client identity); null in phase 1
  "signature": "string|null"                    // reserved (phase 4 signing); null in phase 1
}
```

## L0 checks (phase 1, deterministic, mandatory)

For each cited `evidence_id`:

1. **exists** — the evidence unit exists.
2. **in active version** — belongs to the `graph_version` being served.
3. **acl visible** — the requesting subject is authorised for it (same ACL filter as retrieval).
4. **in requester ledger** — was actually returned to this requester via the retrieval ledger
   (an agent cannot cite evidence it never retrieved).
5. **not stale** — the evidence's source has not been superseded/deleted in the active version.
6. **supporting trust ok** — the evidence is `EXTRACTED`-trust (or backed by an `EXTRACTED` edge);
   an `INFERRED_*` routing hint cannot be the sole support for a claim.

A claim passes L0 iff all its cited evidence passes all checks. `overall = passed` iff every claim
passed; `failed` iff every claim failed; else `partial`.

## Forward-compatibility (no debt for phase 4)

- `client_id` and `signature` are present from v1 (nullable) so phase-4 client identity + signing
  add **values**, not fields — no schema break.
- `verifier_levels_run` is a list so L1–L3 append without restructuring.
- `checks` is an open object keyed by check name; L1/L2/L3 add keys (e.g. `L1_coverage`,
  `L2_typed_fact`, `L3_entailment`) without changing existing ones.
- `claim_results[].result` stays binary; richer L3 detail rides in `checks`/`failed_reasons`.

## Logging

Every `verify_answer` call writes a `retrieval_event` (verification is a broker action). No answer
text or evidence text is logged — only ids, hashes, and check outcomes.
