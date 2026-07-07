# Governance and budgets

An AI agent reading your knowledge base is safe by construction, not by promise. This page
explains the layers that make that true: one door, real identity, fail-closed permissions,
budgets enforced in code, a ledger that is complete by construction, and a citation-grade path
for answers that must be provable.

## One door

Agents never hold database, search, or model credentials. The only way to your knowledge base is
the MCP Context Broker, and everything the broker returns has already been permission-filtered,
budget-charged, and written to the ledger.

Agents keep their native file tools. The knowledge base is preferred-first, never a gate
(ADR-0025): an agent asks the KB first, and reads specific files directly when the KB is missing,
partial, or stale. That fallback is not a leak — the agent is already scoped to its workspace —
and it is measurable: an answered search that returns nothing or nearly nothing is counted in the
dashboard's `kb_search_zero_thin_rate`, the platform's KB-gap signal. Gaps become visible, then
become aliases in the next build.

## Identity

Every tool call is authenticated, and identity binds to the authenticated session — never to
anything the model sends in a request.

- **Local development** uses a loopback-only developer identity. It is not an "auth off" switch:
  every request still flows through the same permission and budget checks. The server logs its
  presence loudly on every start, and refuses to boot with it on a non-loopback address or a real
  tenant id (ADR-0016).
- **Everything else** is fail-closed Entra ID. Tokens are verified against the tenant's public
  JWKS keys — the server holds no client secret — and a token whose signature, issuer, or
  audience does not match is rejected. There is no override.

## Permissions

Team ACLs filter every retrieval surface — search hits, evidence cards, graph traversal at every
hop — and they fail closed: no identity, no results. Derived knowledge can narrow access but
never widen it: an alias or commit artifact carries the intersection of its sources' ACLs. And
revocation is immediate everywhere: a permission removed from a source propagates to every
version still being served, not just future builds.

## Budgets, enforced in code

Budgets are the product's cost model. They make an agent's knowledge spend bounded, predictable,
and inspectable — and they live in the tools, not in prompts. Prompts enforce nothing.

- **`kb_search`** carries a dual cap per session: a call count and a cumulative token budget,
  whichever closes first. When it closes, the tool returns empty results with exactly this
  notice: *"KB budget spent — work with what you have, or read the specific files you still
  need."* That is a contractual outcome, not an error. The agent finishes the job with its file
  tools.
- **`get_task_context`** has its own server-side band (the response is capped and trimmed
  deterministically from the lowest-value tail), separate from `kb_search`'s cap, so the two
  never compete.
- **The governed pack flow** enforces per-run and per-agent budgets under a per-pack lock, with
  justified overruns routed to human approval rather than silently granted.

Every budget outcome — including every refusal — is a ledger row. Raising a budget is a server
configuration change ([tune-budgets](../how-to/tune-budgets.md)), never a prompt change.

## The ledger: complete by construction

Every broker tool call writes exactly one row to the retrieval ledger: answered, reused, denied,
escalated, or failed. Two guarantees matter when you read it:

- **A crashed call still lands in the ledger.** A uniform wrapper ledgers any exception a handler
  did not ledger itself, exactly once, and the error still reaches the caller.
- **A crashed call does not eat budget.** Any charge made before the failure is refunded under
  the same lock the charge used. A failing platform never silently drains an agent's allowance.

The ledger records tokens, statuses, and artifact ids — never query text, never content. What it
looks like and how to read it: [observability](observability.md) and
[query-traces-and-the-ledger](../how-to/query-traces-and-the-ledger.md).

## Retrieved text is untrusted

Everything retrieved from the knowledge base is treated as untrusted content. It cannot change
tool policy, identity, access control, or instructions — the broker's behavior is configured
server-side and nothing in a document can reconfigure it. When an agent opens a card's raw
source, the text arrives in a field literally named `untrusted_content`, scanned by a
deterministic injection check that flags suspicious content but never rewrites it. The decision
of what to do with flagged content stays with the caller, in the open.

## When an answer must be provable

The everyday tools give you sourced answers. Some answers need more: machine-checkable
provenance a host can verify without trusting the agent that wrote them. For those, the broker
serves a deliberate, heavier choreography:

1. **Evidence packs by handle** — one governed retrieval produces a small set of evidence cards,
   deduplicated, reranked, and budget-charged. Raw text is opened per card, on request, metered.
2. **The verifier ladder** — every cited claim is checked in escalating cost: deterministic
   provenance checks first (the evidence exists, is in the served version, is visible to the
   requester, appears in the requester's own retrieval history, is not stale, and is supported by
   an extracted edge), then citation-coverage and typed-fact checks, and only for claims nothing
   deterministic could settle, a cached model entailment check. An unchanged claim never costs a
   second model call.
3. **Signed receipts** — the verdict is issued as an HMAC-signed receipt a host can validate
   statelessly. A claim with no evidence ids is rejected at the schema; missing evidence becomes
   an open question, never an invention.

Client applications are identities too: a registered client carries scopes that gate the tool
surface additively on top of (never instead of) the user's team ACLs, receipts are bound to the
client they were issued for, and a client that requires verification is platform-trusted only
while presenting a valid, matching, passing receipt.

Full shapes: [mcp-tools-contract](../../contracts/mcp-tools-contract.md),
[verification-receipt](../../contracts/verification-receipt.md),
[acl-source-visibility](../../contracts/acl-source-visibility.md).

## Related

- [How your knowledge base is built](how-your-knowledge-base-is-built.md) — where the trust
  vocabulary comes from.
- [Reference: tools](../reference/tools.md) — exact request and response fields.
- [Tune budgets](../how-to/tune-budgets.md) — the knobs, and where they live.
