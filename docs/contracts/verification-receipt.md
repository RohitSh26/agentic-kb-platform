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
  "verifier_levels": ["L0"]                       // requested up to ["L0","L1","L2","L3"]; server runs per policy
}
```

Reject a request with no `claims`, or any claim with no `evidence_ids`, at the schema boundary
(the request never reaches L0/L1 — an uncited claim is a malformed request, not a verification
failure).

`quote` and `assertion` are optional and additive: a phase-1 caller that omits them keeps the exact
behaviour it had. `verifier_levels` defaults to `["L0"]`; higher levels run only when requested (and
admitted by policy). `verifier_levels_run` reflects which levels were **active** for the request
(requested and admitted) — not that each produced a verdict for every claim. A level can be active
yet adjudicate no claim (e.g. L3 is active but every claim was already resolved by L2, so the
entailment model never runs). The authoritative per-claim signal is always the `checks.*` field:
a `null` check means that level produced no verdict for that claim.

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
  "client_id": "string|null",                   // phase 4: the VALIDATED client this receipt was issued to; bound into the signature (scopes the receipt). Null when no client identity was resolved
  "signature": "string|null",                   // phase 4: HMAC-SHA256 over answer_hash+graph_version+client_id+claim_results; null when no signing key is configured
  "key_id": "string|null"                       // phase 4: non-secret fingerprint of the signing key; null when unsigned
}
```

## L0 checks (phase 1, deterministic, mandatory)

For each cited `evidence_id`:

1. **exists** — the evidence unit exists.
2. **in active version** — belongs to the `graph_version` being served.
3. **acl visible** — the requesting subject is authorised for it (same ACL filter as retrieval).
4. **in requester ledger** — was actually returned to this requester via the retrieval ledger,
   *under the served `graph_version`* (an agent cannot cite evidence it never retrieved, nor one it
   retrieved only under a stale/deactivated build).
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
3. **quote-substring guard (invariant 7)** — if the claim carries a within-cap `quote`, that quote
   must be a **verbatim** (whitespace-normalized) substring of the text of at least one of the
   claim's **resolvable cited units** (the same in-version, ACL-visible, requester-retrieved set
   coverage uses — never a unit the requester did not retrieve, so the guard adds no oracle). Both
   sides are whitespace-normalized (runs of whitespace collapsed to a single space) and then an
   EXACT substring test is applied — never fuzzy. A quote grounded in none of the cited units fails
   (`L1_coverage = false`, reason `quote_not_found`). A claim with no quote is unaffected. The guard
   is skipped for an over-cap quote (it has already failed on length; no redundant second reason).

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

## L3 checks (phase 4) — LLM entailment, cached, unresolved-only

Added only when `L3` is requested. L3 is the ONLY non-deterministic level, so it is gated hard by
cost discipline: it runs for a claim **iff L0–L2 produced no deterministic verdict that already
settles it** — concretely, the claim passed every deterministic level that ran but carries no typed
`assertion` L2 could adjudicate (paraphrase / cross-evidence synthesis L0–L2 cannot check). It
**never** runs on a claim L2 already resolved (pass or fail) and never on a claim already failing a
deterministic level. L3 also requires the claim to have ≥1 **resolvable** cited unit (real,
in-version, ACL-visible, requester-retrieved) — the same `resolvable` set L1 uses; a claim with no
resolvable evidence has nothing to entail against and is skipped.

When it runs, the verifier reads the claim's resolvable cited evidence texts (never raw text the
requester did not retrieve) and asks the `EntailmentClient` whether they ENTAIL the claim:
`checks.L3_entailment = true` (pass) or `false` (reason `entailment_unsupported`). A claim L3 does
not run for has `L3_entailment` absent (`null`).

The result is **cached** keyed by `(claim_hash, evidence_ids_hash, prompt_version, model_version)`
in the kb-builder-owned `entailment_cache` table (mcp-server reads/writes it via raw SQL). A cache
hit returns the stored verdict and makes **zero** LLM calls (architecture invariant 4). The local
dev/test model is Ollama (`gemma3:4b`); the backend is swappable behind `EntailmentClient`.

## Signed receipts (phase 4)

The verifier signs the receipt over a canonical serialization of `answer_hash` + `graph_version` +
`client_id` + `claim_results` (each claim reduced to id, result, and its check booleans) using
**HMAC-SHA256**. The key is read at runtime from an environment variable whose NAME is configuration
(default `VERIFY_SIGNING_KEY`); the key VALUE is never a literal in code, fixtures, or logs. The MAC
is written to `signature` and a non-secret key fingerprint to `key_id`.

A host validates a receipt **statelessly** with `verify_receipt_signature(receipt, key,
expected_client_id=...)` — no database, no re-running of checks. Tampering with `answer_hash`,
`graph_version`, `client_id`, or any `claim_results` entry changes the canonical payload and fails
the constant-time MAC comparison. Signing is additive: when no key is configured the verifier still
issues an (unsigned) receipt — L0 stays the mandatory floor; a receipt never requires L3 or a
signature to exist.

## Client identity + official-client enforcement (phase 4, ADR-0011 §6)

A request carries BOTH a per-user subject and a registered **client/app identity** (`client_id` +
`scopes` + `verification_required`), resolved from the authenticated client credential (never a
request field; see `mcp-tools-contract.md`, `MCP_CLIENT_REGISTRY`). The verifier stamps the
**validated** `client_id` into the receipt and **binds it into the signature** — so a receipt is
scoped to the client it was issued to: **a valid receipt for client A does NOT validate for client
B** (cross-client reuse is rejected, even before the MAC check, via `expected_client_id`).

The broker exposes `context.platform_trust`: for a `verification_required` client, an answer is
marked **platform-trusted only** when accompanied by a valid, client-matched, **passing** receipt;
otherwise it returns a clear STRUCTURED denial (`reason ∈ {verification_required_no_receipt,
receipt_unsigned, receipt_client_mismatch, receipt_signature_invalid, receipt_overall_not_passed}`)
— never a silent pass. A client that did **not** opt into `verification_required` gets
`not_required` (its behaviour is unchanged). This gate **composes with** the ACL + trust filters
already enforced on retrieval — client scopes are ADDITIONAL to user-level ACLs, never a replacement.

## Forward-compatibility (no debt for phase 4)

- `client_id` and `signature` are present from v1 (nullable) so phase-4 client identity + signing
  add **values**, not fields — no schema break. PR-32 populates `client_id` (the validated client)
  and includes it in the signed payload.
- `verifier_levels_run` is a list so L1–L3 append without restructuring.
- `checks` is an open object keyed by check name; L1/L2/L3 add keys (e.g. `L1_coverage`,
  `L2_typed_fact`, `L3_entailment`) without changing existing ones.
- `claim_results[].result` stays binary; richer L3 detail rides in `checks`/`failed_reasons`.

## Logging

Every `verify_answer` call writes a `retrieval_event` (verification is a broker action). No answer
text or evidence text is logged — only ids, hashes, and check outcomes.
