# ADR-0025 — KB-first with file-fallback: the knowledge base is a budgeted tool, not a gate

## Status

Accepted (2026-06-18). **Supersedes ADR-0024** (no-shell/RPC-only). Reverses the 2026-06-18
"Use ONLY the Evidence Pack — never roam" manifest change. **Relaxes CLAUDE.md invariants 3 and 6**
(the broker is no longer the *only* way to read code). Driven by the owner after a developer
demo where the broker-mediated agents were slower and worse at coding than a plain agent, and
confirmed by a three-way web-research review (2026-06-18).

## Context

Routing all code reads through the MCP Context Broker — "the broker is the ONLY way to read code"
(invariant 6) and "token saving is enforced by the broker, not prompts" (invariant 3) — crippled the
model. Even Claude 4.6 wrote code slowly and poorly through the mandatory
`create_pack → expand → open_evidence → verify` flow; a plain agent with native `read`/`grep` beat it
outright. Independent research confirmed this is the *documented, expected* outcome, not a tuning bug:

- **Anthropic removed RAG from Claude Code** because agentic search (the model running grep/glob/read)
  "outperformed everything, by a lot." Cursor uses semantic search as *augmentation* on top of grep;
  Sourcegraph Cody deprecated embeddings as the backbone.
- **Governance is enforced at the environment/identity layer, not by removing the model's tools.**
  ACL = an entitlement-scoped workspace/index; audit = post-hoc hooks + telemetry + git provenance.
  "Remove the tools and mediate every read" is a recognized anti-pattern.
- **Prompt caching makes re-reading a file ~90% cheaper**, so "expose evidence by handle to save
  tokens" buys little while adding round-trips. Anthropic now recommends *just-in-time references
  (file paths), not pre-packed evidence*. MCP multi-round-trip retrieval is a known "paper-cut"
  anti-pattern, and MCP tool schemas have near-zero training data.

The owner's direction: **keep the multi-agent orchestration** (an orchestrator that delegates to
specialists), but make the KB a *preferred-first* source rather than a gate — *"use the knowledge
base; if you have enough, don't read the file; if not, then read it"* — and **keep one real
restriction so agents don't get greedy**: a hard cap on how much they can pull. Keep the system
simple.

## Decision

1. **Keep the orchestration.** The orchestrator triages and delegates to specialists; specialists do
   the retrieval and the work. Unchanged.

2. **Agents get native tools back.** In a scoped workspace, specialists have `read`/`grep`/`glob`
   (and `edit` for implementers). The KB is an **optional tool**, never a gate that removes the
   model's hands.

3. **KB-first, file-fallback — a preference expressed in the manifest, not an enforced restriction.**
   For any task the agent starts with the KB (`kb_search` / structural lookups). If the KB gives
   enough to answer, or to know exactly which files matter, it uses it and cites it and does **not**
   re-read what the KB already supplied. If the KB is missing, partial, or stale — or exact current
   code is needed for a change — the agent reads those **specific** files directly. The KB's job is to
   **point at the right place fast** (especially cross-repo); the file read gives **exact truth**.

4. **The one enforced restriction is a budget, in code, not in the prompt.** `kb_search` carries a
   **per-task hard cap** (call count + token budget; reuse the existing token-budget numbers). The
   tool itself counts and, when the cap is hit, stops answering and returns: *"KB budget spent — work
   with what you have, or read the specific files you still need."* This is the only piece of the old
   broker worth keeping: server-side budget enforcement, now applied to **one simple tool** instead of
   a four-step flow. Prompts ask the agent to be efficient; the cap guarantees it.

5. **Drop the complexity.** Remove the mandatory `create_pack → expand → open_evidence → verify`
   pipeline as the required path, the no-shell/RPC-only direction (ADR-0024), and the expose-by-handle
   token optimization (prompt caching covers it). These remain available as *optional* capabilities,
   not the gate.

6. **Governance moves to its natural layers.** ACL = entitlement-scoped workspace/index filter; audit
   = the existing retrieval ledger + harness hooks, **including a log line whenever an agent falls
   back to a file** (a precise signal of where the KB has a gap — the fallback improves the KB over
   time); cost = the `kb_search` budget + prompt caching + spend caps.

## Consequences

- The MVP gets **simpler**: one `kb_search` tool with a budget, a manifest rule (KB-first/file-fallback),
  and a fallback log line. No multi-step flow, no tool removal.
- The model stays fast and smart; the KB becomes an accelerator, not a cage. Nothing already built is
  thrown away — the Postgres graph, Graphify extraction, the connectors (code + docs + ADO tickets),
  the ACL model, and the ledger are all retained; only how they are *exposed* changes (gate → tool).
- The cross-source connectors (search across code **and** docs **and** tickets) become the real
  differentiator a plain grep agent cannot match.
- **CLAUDE.md invariants 3 and 6 must be reworded** (broker = preferred + budgeted, not the sole read
  path). ADR-0024 is superseded; the "never roam" manifest prose is reversed.

## Alternatives considered and rejected

- **Keep the broker as a hard gate (status quo).** Rejected: cripples the model; a governed runtime
  worse than a plain agent has no value.
- **Pure plain agent, no KB.** Rejected: loses the cross-source (code+docs+tickets) grounding and the
  cross-repo "where is X / find references" speed that are the product's reason to exist.
- **Budget enforced by the prompt.** Rejected: prompts don't enforce (the original invariant-3
  lesson). The cap must live in the tool.

## Follow-ups

- Implement the budgeted `kb_search` tool (per-task call + token cap, enforced in the tool; clear
  "budget spent" response).
- Rewrite the agent manifests (canon + `.copilot`/`.opencode` renderings) to the KB-first/file-fallback
  rule; give specialists native tools; extend `check_parity.py` + the parity contract for the native +
  `kb_search` tool surface.
- Log file-fallback events as the KB-gap signal.
- Reword CLAUDE.md invariants 3 and 6; mark ADR-0024 superseded.
- Measure: native-tools + budgeted `kb_search` vs the plain agent vs the old broker on real tasks
  (task #153).
