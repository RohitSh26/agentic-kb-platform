# PR-39 — `get_task_context`: one-call task context on LangGraph (ADR-0030)

## Why

The crown-jewel DX tool: a host agent (OpenCode / Copilot / GH Copilot cloud) makes ONE call with
a task description and gets resolved scope, blast radius, conventions, and similar prior changes —
tiered, cited, budgeted — so it has no reason to fall back to blind file exploration. Schema per
`docs/proposals/2026-07-02-tool-design-first-kb-architecture.md` §2; orchestration per ADR-0030
Decision §2.

**Design rule: zero LLM at query time.** All model work happened in the nightly build; this tool
is pure retrieval + assembly. Fast, cheap, cacheable — the developer's own host model does the
reasoning; we hand it perfect material. LangGraph's value here is parallel fan-out of the
resolution nodes, a bounded low-confidence retry, and LangSmith-traceable structure — not LLM
chains.

## Scope

- **Deps**: `langgraph`, `langchain-core` (and `langsmith` as optional) in mcp-server.
- **Contract first**: `tool_schemas/task_context.py` — request
  `{task_description, hints?{file_paths[], symbols[]}, confidence_floor?, max_tokens?}`; response
  `{resolved_scope{entities[], ambiguous_candidates[]}, blast_radius{callers[], callees[],
  tests[]}, conventions[], similar_prior_changes[], evidence_ids[], budget_used, open_questions[]}`
  — every entity/edge carries `confidence_tier` and hits carry sources. Bump `MCP_SCHEMA_VERSION`
  → 1.10.0; update `docs/contracts/mcp-tools-contract.md` before code.
- **LangGraph StateGraph**: fan-out of four parallel pure-retrieval nodes —
  1. `resolve_scope`: hints first → `alias_reference` lookup (keyword/`search_text`; PR-38 rows if
     present, degrade gracefully to plain search if the KB predates PR-38) → search fallback.
     Ambiguity returns `ambiguous_candidates` + an `open_questions` entry — NEVER a silent guess.
  2. `blast_radius`: `knowledge_edge` traversal (calls/imports/tests) from resolved entities.
     **Confidence rule (implements the 2026-07-02 Graphify audit finding):** a `calls` edge is
     `deterministic` ONLY if corroborated by an import relationship to the target's module;
     otherwise `interpreted` with a `caveat` string. ≥3 name-collision fixtures required (free
     function vs same-named method), all demoted/flagged — none may surface as confident
     `deterministic`.
  3. `conventions`: minimal v1 — rule/ADR/doc artifacts relevant to the resolved scope's
     directories via search; tier `interpreted`.
  4. `similar_prior_changes`: keyword search over commit artifacts.
  → `synthesize` node assembles the response, applies `confidence_floor` filtering, computes
  `open_questions`; conditional edge: if scope resolved empty, ONE broadened retry, then answer
  honestly with what's known.
- **Budget**: response token cap (Evidence Pack band, 6k–8k per `.claude/rules/token-budgets.md`)
  enforced server-side; `budget_used` reported; reuse the existing budget pattern — no second
  mechanism.
- **Ledger + ACL**: `retrieval_event` per call; requester-team filtering on everything returned.
- **LangSmith**: env-gated (`LANGSMITH_TRACING`); the full suite must pass with no LangSmith env
  set (tested).
- **Perf**: integration test on a seeded KB measures and prints p50; loose assert < 5s (target
  < 2s recorded in the plan doc, reported not hard-asserted).
- **A/B harness ships**: `scripts/eval_task_context.py` (two-arm, kb_agent.py-style: tooled vs
  raw) + `evals/agent_task_cases/task_context_ab_v1.yaml` (10 cases, expected-file references
  hand-written before any run). Hermetic assertion: on fixtures, tool output covers the expected
  file set. Live execution needs LLM creds — document how; do not block the PR on it.

## Do NOT

- No LLM calls at query time. None.
- No new ranking SQL — reuse `SearchClient` / existing graph queries.
- Do not modify `kb_search` or any `context.*` tool.
- Do not import kb-builder; do not touch `agents/*.md`.

## Acceptance criteria

- [ ] Contract + schema version bump land before/with the tool; registered in `TOOL_SCHEMAS`.
- [ ] Parallel fan-out + single-retry graph structure tested (nodes run concurrently; retry fires
      only on empty scope).
- [ ] Collision fixtures: 3/3 demoted to `interpreted`+caveat.
- [ ] Ambiguity surfaces as `ambiguous_candidates`/`open_questions`, never a guess (tested).
- [ ] Budget, ledger, ACL tests per house discipline.
- [ ] Suite green with no LangSmith creds; tracing activates via env when present.
- [ ] p50 measured + printed; A/B harness + golden cases ship runnable.
- [ ] `ruff` + `pyright` clean; no excluded-V1 resource.
