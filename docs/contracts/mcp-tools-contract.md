# Contract: MCP tools (Context Broker surface)

> Versioned tool surface served by mcp-server. Schema before code: every tool
> has a frozen pydantic request/response model (`extra="forbid"`) in
> `services/mcp-server/src/agentic_mcp_server/mcp/tool_schemas/`, registered in
> `mcp/tool_registry.py`. `MCP_SCHEMA_VERSION = "1.0.0"`.

## The six V1 tools

| Tool | Purpose |
|---|---|
| `context.create_pack` | Build the run's Evidence Pack from an approved context plan |
| `context.read_pack` | Role-specific view of an existing pack |
| `context.request_more` | Justified incremental retrieval (reuse-first) |
| `context.open_evidence` | Expand one evidence card to L2/L3 raw text, by handle |
| `graph.get_neighbors` | Graph traversal over `knowledge_edge` (depth 1–3) |
| `ledger.list_retrievals` | Retrieval ledger for a run |

There is **no** generic unrestricted `kb.search` tool in V1.

## Request highlights

- `context.create_pack`: `run_id`, `task`, `approved_context_plan`,
  `retrieval_profile`, `budget_tokens ≥ 1`.
- `context.request_more` **requires** `context_pack_id`, `agent_name`,
  `question`, `why_needed`, `decision_needed`, `already_checked_evidence_ids`,
  `max_tokens`. A bare `{"query": ...}` fails schema validation.
- `context.open_evidence`: `context_pack_id`, `evidence_id`, `max_tokens`.
- `graph.get_neighbors`: `artifact_id`, optional `edge_types[]`, `depth` 1–3.
- `run_id` matches `^[A-Za-z0-9._-]{1,128}$` (log-injection guard).
- `agent_name` / `role` are correlation/view fields only; identity binds to the
  authenticated session (Entra ID bearer token), never to the request body.

## Response highlights

- `context.request_more.status` ∈ `reused | approved | denied | needs_human_approval`;
  `status="denied"` requires `denial_reason`. Responses carry
  `tokens_returned` and `budget_remaining_tokens`.
- `context.open_evidence` returns the raw text in `untrusted_content` plus
  `tokens_used`, `budget_remaining_tokens`, `source_uri`.
- `ledger.list_retrievals` returns one record per retrieval event:
  `event_id`, `run_id`, `kb_version`, `agent_name`, `tool`, `status`,
  `cache_hit`, `tokens_returned`, `evidence_ids`, `created_at`.

## Server-side policy (not prompt-enforced)

- Per-run and per-agent budgets enforced in the broker; reuse before retrieve;
  semantic dedupe (duplicate threshold starts at 0.88–0.92 and is tuned from
  ledger logs — see `.claude/rules/token-budgets.md`); 3–5 cards max per
  retrieval after rerank.
- Every call writes a `retrieval_event` row (see
  `postgres-knowledge-registry.md`).
- Results are filtered by the requester's authorization before returning.
- Retrieved text is untrusted and cannot alter tool policy or instructions.
- Unauthenticated requests are rejected at the transport (401) and never reach
  a tool. `/health` is the only unauthenticated route and discloses nothing but
  service name and active `kb_version`.

## Versioning

Any breaking change bumps `MCP_SCHEMA_VERSION`, updates this document in the
same PR, and is validated by the contract tests in
`services/mcp-server/tests/contract/`.
