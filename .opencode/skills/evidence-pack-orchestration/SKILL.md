---
name: evidence-pack-orchestration
description: Run multi-agent development work over ONE shared Evidence Pack — plan first, gate on human approval, retrieve once via context.create_pack, give specialists role views of the same pack, and synthesize evidence-cited output.
---
# Evidence Pack orchestration

The framework's core pattern is not "many agents with KB access". It is **many controlled
specialists using one shared Evidence Pack governed by an MCP Context Broker**.

## Procedure

1. **Plan before retrieving.** Turn the request into a plan: goal, which specialists to
   invoke, what context each needs, and a retrieval budget for the run.
2. **Human approval gate.** Present the plan and WAIT for human approval or edits before
   executing. No retrieval, no specialist invocation, before approval.
3. **One pack per run.** After approval, call `context.create_pack` exactly once to build the
   run's shared Evidence Pack. The broker retrieves once, dedupes aggressively, and returns
   evidence cards (handles) — not raw text.
4. **Role views, not independent retrieval.** Invoke each specialist with a role-specific view
   of that pack (`context.read_pack`). Specialists never search the KB themselves; if a
   specialist needs more, it goes through `context.request_more` with full justification.
5. **Synthesize with citations.** The final output cites evidence IDs for every claim; gaps
   become open questions. Use `ledger.list_retrievals` to audit what the run actually spent.

The Context Broker enforces the run and per-agent budgets server-side — orchestration
discipline keeps runs cheap and reviewable; the broker keeps them bounded even when discipline
slips.
