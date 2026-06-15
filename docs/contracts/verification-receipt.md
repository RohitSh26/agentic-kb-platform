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
    {
      "claim_id": "c1",
      "text": "…",
      "evidence_ids": ["ev_…", "ev_…"],
      "quote": "string|null",                     // optional verbatim span the claim relies on (L1 span cap)
      "assertion": {                              // optional typed assertion the verifier checks (L2)
        "kind": "symbol_in_file | file_imports_module | edge_between",
        "...": "kind-specific fields (see L2 below)"
      }
    }
  ],
  "graph_version": "string|null",                // null ⇒ active version
  "verifier_levels": ["L0"]                       // requested up to ["L0","L1","L2"]; server runs per policy
}
```

Reject a request with no `claims`, or any claim with no `evidence_ids` (that claim fails L1 by
definition; in phase 1 it fails L0 provenance because there is nothing to check).

`quote` and `assertion` are optional and additive: a phase-1 caller that omits them keeps the exact
behaviour it had. `verifier_levels` defaults to `["L0"]`; higher levels run only when requested (and
admitted by policy). `verifier_levels_run` reflects exactly what ran.

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

## The claim/evidence ledger (phase 4)

L1/L2 read a typed, ID-stable **claim/evidence ledger** over the existing tables — it is NOT a new
truth store, it derives nothing: it projects `knowledge_artifact` / `knowledge_edge` + spans into
citeable, deterministically-checkable **fact units**. Each unit has a stable `evidence_id` (the
artifact or edge it reads), a `span` (file path + 1-based inclusive line range, where known), and a
typed assertion the verifier can adjudicate without an LLM. Units are membership- and ACL-filtered
exactly like retrieval — a requester only sees units for artifacts/edges it is authorised for, in
the served version. Unit families:

- **AST facts** — `symbol_in_file` (a `code_symbol`/`test` artifact named X with source path F and a
  line span), `file_imports_module` (an `imports` edge from file F to module M), `symbol_calls`
  (a `calls` edge from symbol A to symbol B).
- **prose facts** — a `doc`/`concept` artifact's statement and its source span.
- **edge facts** — an edge of type T between artifacts A and B (the relation + its evidence pointer).

## L1 checks (phase 4, deterministic) — citation coverage + span caps

Added only when `L1` is requested. Per claim:

1. **coverage** — the claim cites ≥1 evidence unit that resolves to a real, in-version,
   ACL-visible, requester-retrieved ledger unit. A claim citing nothing checkable (all ids unknown /
   invisible) fails coverage (`L1_coverage = false`, reason `claim_uncited`).
2. **span cap** — if the claim carries a `quote`, its length is within the configured cap
   (`BrokerSettings.max_quote_chars`). An over-cap quote fails (`L1_coverage = false`, reason
   `quote_over_cap`). A claim with no quote is not penalised on this check.

`checks.L1_coverage` is added to every claim's `checks` when L1 runs.

## L2 checks (phase 4, deterministic, NO LLM) — typed-fact adjudication

Added only when `L2` is requested AND the claim carries a typed `assertion`. The verifier resolves
the matching ledger unit and checks the claim's assertion against it. This catches the case L0 alone
misses: the cited evidence is real and retrieved, but the claim **misreads** it.

| `assertion.kind`      | fields                                  | passes iff a ledger unit shows …                                  |
|-----------------------|-----------------------------------------|------------------------------------------------------------------|
| `symbol_in_file`      | `symbol`, `file`                        | a `code_symbol`/`test` named `symbol` whose source path is `file`|
| `file_imports_module` | `file`, `module`                        | an `imports` edge from file `file` to a module named `module`    |
| `edge_between`        | `edge_type`, `from_id`, `to_id`         | an edge of type `edge_type` between artifacts `from_id`/`to_id`  |

`checks.L2_typed_fact` is `true` when the assertion matches a ledger unit, `false` when it does not
(reason `typed_fact_unsupported`). A claim with no `assertion` is **not adjudicated** by L2 (the key
is omitted from its `checks`); L2 never invents a verdict it cannot deterministically support.

A claim's overall `result` is the AND of every level that ran and produced a verdict for it: L0 (if
run), L1 coverage/span (if L1 run), and L2 typed-fact (only for claims carrying an assertion).

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
