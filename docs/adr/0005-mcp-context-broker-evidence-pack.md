# 0005. MCP Context Broker + shared Evidence Pack

- Status: Accepted
- Date: 2026-06-10
- Deciders: Platform team

## Context
"Many agents with KB access" causes over-retrieval, inconsistent worldviews, and token blowup. We
need controlled specialists sharing one governed context object.

## Decision
The MCP server is a Context Broker: it creates a run-scoped shared Evidence Pack, enforces per-run and
per-agent budgets server-side, returns evidence cards before raw text, deduplicates queries, exposes
graph expansion, and records provenance in a retrieval ledger. Subagents read the pack first and may
request only justified deltas.

## Consequences
+ Token saving is an enforced architectural behavior, not a prompt suggestion.
+ Consistent shared context; auditable retrieval; measurable efficiency.
- More server-side logic and schema work up front (budgets, dedupe, ledger).

## Alternatives considered
Thin wrapper over Azure AI Search (no policy, no budgets, no shared context) — rejected.
