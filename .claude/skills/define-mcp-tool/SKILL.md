---
name: define-mcp-tool
description: >
  Workflow for adding or changing an MCP Context Broker tool (context.*, graph.*, ledger.*). Use
  before implementing any tool so the request/response schema, budgets, and provenance are designed
  first. Triggers: "add an MCP tool", "context broker tool", "create_pack", "request_more".
---

# Define an MCP tool (contract first)

1. **Schema before code.** Add a versioned request and response schema to
   `packages/contracts/mcp_schemas/`. No implementation until the schema is reviewed.
2. **Budgets are server-side.** The tool enforces per-run and per-agent token budgets itself. A
   prompt asking nicely is not enforcement.
3. **Evidence by handle.** Return L0/L1 evidence cards first. Raw chunks (L2+) only when a handle is
   explicitly expanded via `context.open_evidence`.
4. **Justified deltas only.** `context.request_more` requires `question`, `why_needed`,
   `decision_needed`, `already_checked`, and `max_tokens`. Reject a bare `{"query": "..."}`.
   Possible statuses: `reused | approved | denied | needs_human_approval`.
5. **Provenance.** Every call writes a `retrieval_event` (source, artifact ids, kb_version, agent,
   tokens, cache_hit, semantic_reuse, latency).
6. **Untrusted content.** Wrap retrieved text so it cannot change tool policy, identity, or access
   control.
7. **Tests** (use `test-author`): budget exceeded, exact-cache hit, semantic reuse, evidence
   expansion, and an injection-style document that must NOT alter behavior.
8. Have `mcp-contract-reviewer` sign off before merge.

Reference tool set and I/O shapes: `docs/architecture/00-overview.md` §"MCP Context Broker".
