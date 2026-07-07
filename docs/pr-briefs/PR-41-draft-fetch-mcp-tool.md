# PR-41 — `get_review_draft`: fetch review drafts over MCP (ADR-0031 follow-up)

## Why

The dev-gated review flow's v1 fetch path is a CLI — fine for OpenCode (has a shell), unusable
from VS Code Copilot chat. ADR-0031 named this follow-up: an MCP tool so the developer's
`code_reviewer` agent can pull the panel's stored draft into any host session.

## Scope

- **Contract first**: add the tool to `docs/contracts/mcp-tools-contract.md` (bump
  `MCP_SCHEMA_VERSION` minor) and a "Fetching drafts over MCP" section to
  `docs/contracts/review-panel.md`. Request: `{repo, pr_number, head_sha?}` (omitted sha = latest
  draft for the PR). Response: the stored draft (draft key, created_at, findings, summary_markdown)
  or a clean not-found envelope — never an exception for "no draft yet".
- **Read-only, compute-never**: mcp-server reads the `review_panel` schema (same documented
  cross-schema READ pattern as the registry; review-panel remains the sole writer). The tool NEVER
  triggers draft computation — the server keeps its zero-LLM invariant; computing stays with the
  engine/CLI.
- **Governance**: one `retrieval_event` per call (approved/error — the house guarantee). No
  kb-search budget charge (this is not knowledge retrieval; record that decision in the contract).
  Authenticated subject required; drafts are visible to any authenticated requester in v1
  (single-team local scope — note the multi-team ACL question in the contract as a layer-2 item).
- **Manifest grant**: `agents/code_reviewer.md` gains `get_review_draft` in `allowed_tools`
  (version bump), body updated ("pull the draft" now names the tool), renderings propagated,
  parity exit 0. Host allowlists: add to `.copilot/mcp/repository-settings.json` union and the
  OpenCode grants for code_reviewer.

## Do NOT

- No LLM calls, no draft computation, no writes to any review_panel or registry table.
- Do not change the CLI path or the engine.

## Acceptance

- [ ] Contract + schema bump before code; registered in TOOL_SCHEMAS and reachable (handler wired
      — the run-1 lesson: registered ≠ reachable; entrypoint test).
- [ ] Tests: draft exists → returned intact; no draft → clean not-found; one ledger row per call
      incl. the not-found and error paths; read-only asserted; no budget charge asserted.
- [ ] Parity 12/12; mcp-server + review-panel contract tests green; ruff/format/pyright clean.
