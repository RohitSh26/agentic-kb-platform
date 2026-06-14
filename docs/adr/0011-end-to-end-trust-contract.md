# ADR-0011 — End-to-end trust contract: trust buckets, claim/evidence ledger, verifier, signed receipts

## Status

Accepted (2026-06-14)

## Context

The Context Broker governs *retrieval* — ACLs, budgets, evidence-by-handle, untrusted-content
flagging. It does **not** govern the *agent's final answer*. External agents we don't control
(GitHub Copilot, Claude, OpenCode) can ignore the evidence, paraphrase it wrongly, or cite an
artifact that doesn't support the claim. The biggest hole in the prior design was the absence of a
trust/evaluation contract spanning **source → graph → retrieval → answer**.

A second external-judge round addressed "how do we make citations trustworthy?". Its rulings:

- **You cannot enforce trust against agents you don't control.** The only enforceable boundary is:
  *only an answer carrying a valid verification receipt is **platform-trusted**.* The host/client
  decides whether to require one; the platform decides what "verified" means.
- Reject **broker-generated answers** as the default (turns the broker into a chatbot, breaks
  "queries are ~free", couples us to a model). Reject a **"mandatory" verifier with no teeth**
  (fake unless a client enforces it).
- The answer is **Option 3**: a claim/evidence ledger + a verifier tool + a **signed verification
  receipt** + **client/app identity** so a host can enforce "platform-trusted ⇒ has a valid
  receipt".
- **Trust must be enforcement, not decoration.** Use **trust buckets**, not decimal confidence.
- **Pull the trust contract early (phase 0/1), not late.** Don't expose `INFERRED` edges until the
  broker already treats them as lower-trust routing hints.

## Decision

### 1. Trust buckets (not decimal confidence)

Every edge and every citeable fact carries one of:
`EXTRACTED` · `INFERRED_HIGH` · `INFERRED_LOW` · `AMBIGUOUS` · `REJECTED`
(defined in `docs/contracts/trust-buckets.md`). Derivation is deterministic from the producing
mechanism + evidence rule, never a free-floating score. **`AMBIGUOUS` and `REJECTED` are excluded
from default traversal and from supporting a final claim.**

### 2. Trust-aware traversal

`graph.get_neighbors` (and future graph tools) take `trust_floor`, default `EXTRACTED`. An
`INFERRED` edge is a **routing hint to source evidence, never truth itself** — it can point an agent
at a card to read, but cannot itself support a cited claim.

### 3. Claim / evidence ledger

Citeable evidence units are typed and ID-stable: AST facts, prose facts (with source spans),
edge facts. An answer references them by ID. This is a model over the existing
`knowledge_artifact` / `knowledge_edge` / source-span data, not a new truth store. Built out fully
in phase 4; phase 1 ships the minimal provenance slice.

### 4. Layered verifier (cheap-first)

A broker tool verifies an answer's citations against the ledger, in escalating cost:

- **L0 — access + provenance (deterministic, mandatory, cheap):** every cited evidence ID exists, is
  in the active `graph_version`, was visible to the requester (ACL), appears in their retrieval
  ledger, and is not stale. **Ships in phase 1.**
- **L1 — citation coverage + span caps:** each claim cites ≥1 evidence; quoted spans within caps.
- **L2 — typed-fact checks (from the ledger, no LLM):** deterministic checks for fact types the
  ledger can adjudicate.
- **L3 — LLM entailment (cached):** only for claims that cannot be checked deterministically.

### 5. Signed verification receipt

The verifier returns a signed receipt: `answer_hash`, the per-claim results, the verifier levels
run, `graph_version`, and a signature. Schema in `docs/contracts/verification-receipt.md`. Phase 1
emits a minimal (L0) receipt; phase 4 adds signing + L1–L3 + temporal results.

### 6. Client / app identity + scopes

Per-user bearer identity is not enough to enforce "platform-trusted". The broker recognises a
registered **client/app identity** with scopes and a `verification_required` policy, so a host can
enforce that only receipt-bearing answers are surfaced as platform-trusted. Auth model extension in
phase 4; the receipt schema reserves the fields from phase 0.

### 7. Temporal semantics

Evidence carries recency/state so the broker can prioritise by query intent: "how does X work?"
prioritises current code; "why was X changed?" includes cards / PRs / ADRs. Specified in phase 0,
enforced in phase 4.

## Consequences

- Trust is **load-bearing from phase 1**: `trust_floor=EXTRACTED` default + an L0 verifier exist
  before any `INFERRED` edge is ever produced (phase 3).
- The quote-substring guard (invariant 7) is necessary but **not sufficient** — a correct quote can
  still be misread — so important fact types get evals + L2/L3 entailment.
- **Evidence-recall is a first-class metric and a publish gate** (`docs/contracts/publish-gates.md`),
  because the silent failure mode is underlinking, not wrong citations.

## Alternatives rejected

- **Broker-generated answers as default:** breaks the cost model and the host-agnostic boundary.
- **Decimal confidence scores:** uncalibrated, invite false precision; buckets drive behaviour.
- **A verifier with no client enforcement:** unenforceable against external agents; the receipt +
  client-identity pairing is what makes "platform-trusted" meaningful.

## Relationships

Implements the trust half of ADR-0010. Builds on ADR-0005 (Context Broker / Evidence Pack),
ADR-0002/0003 (Postgres graph). Contracts: `relation-ontology.md`, `trust-buckets.md`,
`verification-receipt.md`, `acl-source-visibility.md`, `golden-query-evals.md`, `publish-gates.md`.
