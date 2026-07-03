# PR-37 — Ship the real `kb_search` MCP tool (ADR-0025, ADR-0030)

## Why

ADR-0025 (2026-06-18) decided the knowledge base should be exposed as ONE budgeted `kb_search` tool,
not the mandatory `create_pack → expand → open_evidence → verify` broker flow. `scripts/kb_agent.py`
proves the pattern works (`docs/reports/kb-benefit-2026-06-18.md`: 3/3 correct + cited vs. 0/3, all
hallucinated, on a no-KB baseline). But `services/mcp-server/src/agentic_mcp_server/mcp/tool_registry.py`'s
`TOOL_SCHEMAS` has no `kb_search` entry — only the old broker tools. Every one of the twelve agent
roles in `agents/*.md` (ADR-0030) grants `kb_search` and depends on it existing. This is the actual
foundational gap this platform's whole recent rethink has been circling — nothing downstream of it
is real until this lands.

## Scope

- **New tool schema** (`services/mcp-server/src/agentic_mcp_server/mcp/tool_schemas/search.py`):
  `KbSearchRequest` (`query: str`), `KbSearchResponse` (`results: list[KbSearchHit]`,
  `budget_remaining: {calls: int, tokens: int}`). Each `KbSearchHit` carries `title`, `artifact_type`,
  `source_uri`, `snippet`, and a `confidence_tier` field
  (`docs/proposals/2026-07-02-v2-world-class-platform-architecture.md`'s tiering:
  `ground_truth`/`deterministic`/`interpreted`) — keyword-search hits start at `interpreted` (they
  are not yet cross-validated, per this session's Graphify confidence-tiering finding); leave a clear
  extension point for graph-derived hits to carry `deterministic` once blast-radius wiring lands in a
  follow-up PR.
- **Handler** (`tool_handlers.py`): calls the **existing** `PostgresKeywordSearchClient.search()`
  (`infrastructure/postgres/keyword_search.py`) — do not write new search or ranking SQL; that logic
  already exists, is IDF-weighted, and title/search_text/body-boosted. The handler's job is budget
  enforcement and response shaping only.
- **Budget enforcement, server-side** (ADR-0025 §4 — the one enforced restriction): a per-run
  call-count cap AND a per-run token cap on cumulative KB content returned, mirroring the existing
  budget-enforcement pattern already used for the `context.*` tools (`context_broker/budgets.py`,
  `domain/token_budget.py`) — read those first and reuse the pattern; do not invent a second budget
  mechanism. Numbers from `.claude/rules/token-budgets.md`, matching the per-role budgets already
  declared in each `agents/*.md` frontmatter this session wrote.
- **Register** `kb_search` in `TOOL_SCHEMAS` (`tool_registry.py`) and wire it in `server.py` alongside
  the existing tools — ADD, do not remove any `context.*` entry; ADR-0025 keeps them available as
  optional, not the gate.
- **Tests** (the point of the PR): budget cap enforced on BOTH call count and token count (matches
  `kb_agent.py`'s proven dual-cap `_kb_budget_open` logic — one without the other is a bug); a
  `retrieval_event` row is written per call (existing ledger discipline, `mcp-tools.md` rule); ACL
  filtering applies to returned results, same as every other tool. Ranking/relevance itself needs no
  new tests — that surface is `PostgresKeywordSearchClient`'s, already covered where it's tested.

## Do NOT

- Do not remove or deprecate any `context.*` tool — ADR-0025 keeps them optional; this PR only ADDS
  `kb_search` alongside them.
- Do not write new search or ranking logic — `PostgresKeywordSearchClient` already exists and is the
  backend; this PR wires it behind a budgeted tool, it does not reimplement it.
- Do not build the `get_task_context` tool in this PR — that's blast-radius + alias-index +
  conventions layered on top of `kb_search`, a separate, larger PR once `kb_search` itself is proven
  (matches the phased build plan's own Phase 1/Phase 2 split).
- Do not touch `agents/*.md` — the manifests already grant `kb_search`; this PR makes the grant real,
  it does not change who gets it.

## Acceptance criteria

- [ ] `kb_search` tool schema (request/response, `confidence_tier` field) added and registered in
      `TOOL_SCHEMAS`.
- [ ] Handler calls the existing `PostgresKeywordSearchClient`; no new ranking SQL written.
- [ ] Budget enforced server-side: call-count cap AND token cap, both required; the tool response
      states remaining budget.
- [ ] A `retrieval_event` row is written per call; ACL filtering applies to returned results.
- [ ] Existing `context.*` tools unchanged and still registered.
- [ ] Check whether `test_agent_manifests.py` (the pre-existing failure this session's parity work
      surfaced) passes once this lands, or is testing something else — verify, don't assume.
- [ ] `ruff` + `pyright` clean; tests green; no excluded-V1 resource added.
