# Contract: MCP tools (Context Broker surface)

> Versioned tool surface served by mcp-server. Schema before code: every tool
> has a frozen pydantic request/response model (`extra="forbid"`) in
> `services/mcp-server/src/agentic_mcp_server/mcp/tool_schemas/`, registered in
> `mcp/tool_registry.py`. `MCP_SCHEMA_VERSION = "1.2.0"` (1.1.0 = PR-13:
> `authorization` decision on every retrieval response, `injection_*` markers
> on cards and expansions; 1.2.0 = PR-18: `read_pack.role` opened from the
> closed six-role enum to a free-form charset-guarded string — response
> consumers may now receive team-defined role values).

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
- `context.read_pack.role` is **free-form** — adopting teams name their own
  roles (`security_auditor` is as valid as `implementation`); the broker never
  branches on it. It matches `^[A-Za-z0-9._-]{1,64}$` because the value lands
  verbatim in `key=value` audit logs (same forgery guard as `run_id`).
- `agent_name` / `role` are correlation/view fields only; identity binds to the
  authenticated session (Entra ID bearer token), never to the request body.

## Response highlights

- `context.request_more.status` ∈ `reused | approved | denied | needs_human_approval`;
  `status="denied"` requires `denial_reason`. Responses carry
  `tokens_returned` and `budget_remaining_tokens`.
- Status semantics (V1): `reused` = the question matched a previous retrieval
  (exact normalized match or semantic similarity ≥ threshold) and existing
  evidence is returned at no budget cost; `denied` = the requesting agent has
  exhausted its per-agent request count or token allowance; `needs_human_approval`
  = the request itself is justified but would exceed the remaining per-run
  budget — a human can raise the budget; `approved` = new evidence retrieved
  and charged.
- `evidence_id` is the artifact UUID rendered as a string in V1 — stable
  within a pack and storable in the ledger's UUID-array columns.
- Unknown `context_pack_id` or `evidence_id` ⇒ tool error plus a ledger row
  with the ledger-only status `error` (the broker holds pack state in memory
  per instance in V1; the durable record is the ledger). The same applies when
  no active `kb_version` exists. `error` rows use the sentinel `"-"` for
  `run_id`/`kb_version` values the broker could not resolve; `graph.get_neighbors`
  rows also use `run_id = "-"` because graph lookups are not run-scoped.
- `context.read_pack` charges no budget (reuse is the point); `create_pack`,
  `request_more`, and `open_evidence` charge the budgets they consume.
  `open_evidence` enforces both the per-run budget **and** the per-agent token
  allowance; exceeding either ⇒ tool error plus a `denied` ledger row.
- Per-agent allowances are keyed by the **authenticated session subject** and
  supplied per deployment via the optional `MCP_AGENT_ALLOWANCES` env var — a
  JSON object `{subject: {max_requests, max_tokens}}` (identifiers only, never
  secrets). Unlisted subjects get the conservative default (1 request / 2,500
  tokens). `max_requests: 0` is valid and means the subject may never
  `request_more`. Malformed config (bad JSON, padded or duplicate subject
  keys, non-integer values) fails the boot — it never silently defaults.
- `context.open_evidence` returns the raw text in `untrusted_content` plus
  `tokens_used`, `budget_remaining_tokens`, `source_uri`.
- Evidence card `title` and `summary` fields are derived from retrieved
  content: treat them as untrusted text, the same as `untrusted_content`.
- Every retrieval response (`create_pack`, `read_pack`, `request_more`,
  `open_evidence`, `get_neighbors`) carries an `authorization` decision:
  `{policy, decision}` where `policy` names the active filter (V1:
  `team_acl_v1`) and `decision` is always `allowed` — unauthorized artifacts
  are silently removed before ranking, and the response deliberately carries
  **no filtered count** (counts would leak the existence of restricted
  artifacts). A fully-denied `open_evidence` is a tool error
  (`evidence not available`) — the same error as a missing artifact or an id
  that was never in the pack, so none of the three is distinguishable.
- Evidence cards carry `injection_flagged: bool` + `injection_signals: list[str]`
  (scanned over title + summary); `open_evidence` responses carry the same pair
  scanned over the expanded body. Flagging is advisory and deterministic
  (regex, no model calls); flagged content is returned **verbatim**, never
  rewritten — the consumer decides how to treat it, the broker never lets it
  alter policy.
- `ledger.list_retrievals` returns one record per retrieval event:
  `event_id`, `run_id`, `kb_version`, `agent_name`, `tool`, `status`,
  `cache_hit`, `tokens_returned`, `evidence_ids`, `created_at`. The non-run
  sentinel `run_id = "-"` is rejected (it aggregates every subject's
  non-run-scoped activity and is operator-only). V1 accepts that ledger
  records are visible to any authenticated subject that knows the `run_id`
  (artifact UUIDs in `evidence_ids` confirm existence); run-scoped ledger
  authorization is a recorded follow-up, not a V1 guarantee.

## Server-side policy (not prompt-enforced)

- Per-run and per-agent budgets enforced in the broker; the run budget
  requested by `create_pack` is clamped to a server-side maximum (default
  18k, the top of the 12k–18k band) — the request value is never an escape
  hatch; reuse before retrieve;
  semantic dedupe (duplicate threshold starts at 0.88–0.92 and is tuned from
  ledger logs — see `.claude/rules/token-budgets.md`); 3–5 cards max per
  retrieval after rerank. Per-agent identity binds to the authenticated
  session subject, never to `agent_name`.
- The broker makes **no LLM or embedding calls** in V1: pack summaries are
  assembled from registry artifacts, and semantic dedupe is a deterministic
  token-similarity measure. Retrieval relevance goes through the `SearchClient`
  interface (Postgres keyword implementation locally; the Azure AI Search
  implementation stays behind the same interface).
- Every call writes a `retrieval_event` row (see
  `postgres-knowledge-registry.md`).
- Results are filtered by the requester's authorization before returning
  (PR-13: `team_acl_v1`). The requester is the authenticated session subject
  plus its team set, taken from the bearer token's `groups`/`roles` claims —
  never from the request body. An artifact with empty `acl_teams` is
  org-public (any authenticated subject); a non-empty `acl_teams` requires a
  non-empty intersection with the requester's teams. Filtering applies at
  every surface: card retrieval, pack reads (`read_pack` re-filters the
  cached cards against the reading requester), reuse (`request_more` reused
  ids are re-filtered for the caller; a fully-suppressed reuse falls through
  to a fresh, filtered retrieval), evidence expansion
  (`open_evidence` re-hydrates from Postgres and re-filters — a pack handle
  is not a grant), and graph traversal, where the root node and each BFS hop
  are filtered **before** expanding the frontier so restricted nodes never
  reveal their connectivity — an unauthorized root returns the same empty
  result as an unknown id.
- Retrieved text is untrusted and cannot alter tool policy or instructions.
  The broker scans retrieved text for injection patterns (instruction
  overrides, role markers, chat-template tokens, secret-exfiltration asks,
  unicode direction/zero-width tricks) and marks matches via the
  `injection_*` response fields; detections are audit-logged.
- Every context expansion and source access is audit-logged to structured
  stdout (`telemetry/audit.py`): requester subject + teams, tool, artifact
  ids, ACL-suppressed artifact ids, and injection detections. Audit lines
  carry ids and metadata only — never `body_text`. The audit stream is
  operator telemetry; the Postgres `retrieval_event` ledger remains the
  agent-visible durable record.
- Unauthenticated requests are rejected at the transport (401) and never reach
  a tool. `/health` is the only unauthenticated route and discloses nothing but
  service name and active `kb_version`.

## Versioning

Any breaking change bumps `MCP_SCHEMA_VERSION`, updates this document in the
same PR, and is validated by the contract tests in
`services/mcp-server/tests/contract/`.
