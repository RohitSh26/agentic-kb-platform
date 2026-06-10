# PR-10 — Context Broker

## Scope
Implement the broker tools: context.create_pack, context.read_pack, context.request_more,
context.open_evidence, plus graph.get_neighbors and ledger.list_retrievals. Budgets, dedupe, evidence
levels, provenance.

## Context
docs/architecture §8–10. ADR-0005. .claude/rules/{mcp-tools,token-budgets}.md. Use the define-mcp-tool
skill.

## Files to create
- `services/mcp-server/src/agentic_mcp_server/context_broker/{pack.py,request_more.py,evidence.py,budgets.py,dedupe.py}`.
- `context_broker/graph.py`, `context_broker/ledger.py`.

## Contracts
Exact tool I/O per docs/architecture §8; request_more rejects bare {"query": "..."}; statuses
{approved, reused, denied, needs_human_approval}.

## Acceptance criteria
- Evidence cards (L0/L1) returned before raw chunks; raw via open_evidence by handle only.
- Per-run AND per-agent budgets enforced server-side; exceeding ⇒ denied + ledger entry.
- Exact-query cache hit and semantic reuse both work (threshold ~0.88–0.92, configurable).
- Every retrieval writes a retrieval_event with provenance + tokens + cache flags.
- Use Postgres as truth; reach Search only through SearchClient.

## Required tests
- exact cache hit, semantic reuse, budget exceeded (run + agent), evidence expansion, and an
  injection-style document that must NOT alter behavior.

## Do NOT
- Implement Wikify/Graphify. No Redis, Service Bus, or Blob. No "think by retrieving" path.

## Kickoff prompt
"Implement PR-10 using define-mcp-tool. Contracts first, budgets/dedupe/evidence/provenance server-side.
Full test matrix incl. injection doc. Run mcp-contract-reviewer and security-auditor before done."
