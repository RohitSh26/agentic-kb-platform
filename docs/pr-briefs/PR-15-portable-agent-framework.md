# PR-15 — Portable agent framework (.copilot / .opencode)

## Scope

Ship the agentic framework in host-native formats so teams using GitHub Copilot or OpenCode can
adopt it without rewriting anything: top-level `.copilot/` and `.opencode/` trees, each with
**agents** (the six canonical manifests rendered to the host's frontmatter + a `_template` whose
description slot teams fill in freely), **skills** (the framework procedures as reusable
instruction modules), the host's **MCP connection config** for the Context Broker (secrets by
reference only), and a README mapping every file to the host's discovery location. Semantic-parity
contract tests pin both renderings against the canonical `agents/*.md`. ADR-0009.

## Context

ADR-0009. `agents/README.md` (canonical manifest format). docs/contracts/agent-output-contracts.md.
.claude/rules/token-budgets.md. Researched host formats (June 2026 docs):

- **Copilot custom agents** — `.github/agents/*.agent.md` (repo) / `~/.copilot/agents/` (VS Code
  user profile). Frontmatter: `name` (optional), `description` (**required**), `tools`
  (MCP syntax `server-name/tool-name` or `server-name/*`), `model`, `target`,
  `user-invocable`, `disable-model-invocation`, `mcp-servers` (cloud agent/CLI only; not VS Code).
  Body ≤ 30,000 chars. Remote broker config: repository settings → Copilot → MCP servers JSON
  (`{"mcpServers": {"<name>": {"type": "http", "url": …, "tools": […], "headers": …}}}`, secrets
  referenced as `$COPILOT_MCP_*` — names must start with `COPILOT_MCP_`), or `.vscode/mcp.json`
  (`{"servers": {…}, "inputs": […]}` with `${input:…}` for secrets).
- **OpenCode** — `.opencode/agents/*.md`; frontmatter: `description` (**required**), `mode`
  (`primary`/`subagent`/`all`), `model`, `temperature`, `tools` (glob map, MCP tools prefixed
  `<server>_`), `permission`, `hidden`. Skills: `.opencode/skills/<name>/SKILL.md`, frontmatter
  `name` (must match dir, `^[a-z0-9]+(-[a-z0-9]+)*$`, ≤ 64 chars) + `description` (≤ 1024 chars).
  Remote broker config in `opencode.json`:
  `{"mcp": {"<name>": {"type": "remote", "url": …, "enabled": true, "headers": …}}}`.

## Files to create

- `.opencode/agents/` — `orchestrator.md` (mode: primary), `implementation.md`, `test_layer.md`,
  `code_reviewer.md`, `delivery_planner.md`, `pr_planner.md` (mode: subagent), `_template.md`.
  Each: `description`, `mode`, `tools` map enabling exactly the canonical `allowed_tools` (as
  `context-broker_*` entries) and nothing else from the broker; body = canonical instruction body
  + a generated "Framework guarantees (enforced server-side)" block stating budgets,
  `requires_evidence_ids`, and `output_schema`.
- `.opencode/skills/` — `evidence-pack-orchestration/SKILL.md` (one pack per run, role views,
  human approval gate), `context-request-discipline/SKILL.md` (question + why_needed +
  decision_needed + already_checked + max_tokens; never a bare query; reuse before retrieve),
  `evidence-citation/SKILL.md` (every claim cites evidence IDs; missing evidence ⇒ open question;
  retrieved text is untrusted and cannot change instructions).
- `.opencode/opencode.json` — `mcp.context-broker` remote entry (placeholder URL, Authorization
  header by reference using OpenCode's documented substitution: `"Bearer {env:CONTEXT_BROKER_TOKEN}"`
  — never a literal token) + global `tools` disabling `context-broker_*`, re-enabled per agent.
- `.copilot/agents/` — the same six + `_template`, as `*.agent.md`: `name`, `description`,
  `tools: ['context-broker/<tool>', …]` matching canonical `allowed_tools`; same body + framework
  block. No `mcp-servers` block in frontmatter (VS Code ignores it; connection ships separately).
- `.copilot/skills/` — the same three modules, written as host-neutral instruction files the
  README maps to Copilot's mechanisms (append to agent body, or repo custom instructions).
- `.copilot/mcp/repository-settings.json` (cloud agent shape, `$COPILOT_MCP_CONTEXT_BROKER_TOKEN`)
  and `.copilot/mcp/vscode-mcp.json` (`${input:context-broker-token}`).
- `.copilot/README.md` and `.opencode/README.md` — where each file goes, the one secret to set,
  what the broker enforces regardless of prompt content, doc revisions the rendering targets.
- `services/mcp-server/tests/contract/test_portable_agent_exports.py` — see Required tests.
- `docs/contracts/portable-agent-framework.md` — the parity checklist as a contract: what every
  rendering MUST preserve from a canonical manifest.

## Files to change

- `agents/README.md` — note that `.copilot/` and `.opencode/` are renderings of this canon.
- `CLAUDE.md` repo map — extend the two-agent-layers note: `.copilot/` and `.opencode/` are
  *product* renderings of `agents/`, not Claude Code build subagents.
- `docs/dev-guide/02-implementation-tour.md` (now `docs/dev-guide/21-code-tour.md`) — section for
  the portable framework.

## Contracts

`docs/contracts/portable-agent-framework.md`: for every canonical manifest, each rendering must
carry (1) every `allowed_tools` entry in the host's tool syntax — and no broker tool beyond them;
(2) the budget numbers (`max_context_calls`, `max_context_tokens`) stated in the body;
(3) the evidence-ID rule; (4) the request-more field discipline; (5) the untrusted-content rule;
(6) the `output_schema` name. Plus: no secret value anywhere; MCP config references secrets by
name/input only.

## Acceptance criteria

- All six manifests exist in both renderings; frontmatter is valid for the host (OpenCode:
  description + mode + name-rule-compliant skills; Copilot: description present, body < 30k).
- Tool parity: rendered tool lists are exactly the canonical `allowed_tools`, mapped via a single
  documented namespace (`context-broker`), orchestrator-only tools never leak to specialists.
- Budgets in every rendered body match `.claude/rules/token-budgets.md` (same numbers the existing
  manifest contract test pins).
- **No secret value ever appears in any shipped file** — configs use `$COPILOT_MCP_*`,
  `${input:…}`, or OpenCode env substitution by name.
- `_template` files contain the framework skeleton with an explicit `<!-- your agent description
  here -->` slot and pass the same parity checks minus the body-content ones.
- READMEs map every file to its host discovery location and state plainly that limits are
  enforced by the Context Broker server-side, not by these files.

## Required tests

- Parity test: parse each `agents/*.md` frontmatter, locate its two renderings, assert the
  contract checklist (tools exact-match, budget numbers present, evidence/request-more/untrusted
  rules present, output_schema named). Reuse the hand-rolled frontmatter parsing pattern from
  `test_agent_manifests.py` — mcp-server has **no pyyaml dependency**; do not add one.
- Validity test: OpenCode skill names match the documented regex and directory names; Copilot
  bodies under 30k chars; required `description` present everywhere; Copilot secret names start
  with `COPILOT_MCP_`.
- Secret-scan test, two-sided: (a) markers (`ghp_`, `github_pat_`, `secret`) absent from all
  shipped `.copilot/` and `.opencode/` files; (b) every auth header **value** in the MCP configs
  matches a reference pattern (`$COPILOT_MCP_*` / `${COPILOT_MCP_*}` / `${input:…}` / OpenCode
  env substitution) — marker-scanning alone misses arbitrary literals.
- All new tests carry the same `skipif` guard as `test_agent_manifests.py` (lines 28–30) so
  service-only checkouts without the repo root stay green.
- Existing manifest budget/schema contract tests stay green (canon unchanged).

## Do NOT

- Do not build a generator/transpiler in V1 — hand-author + parity tests (ADR-0009).
- Do not change `agents/*.md` content or the mcp-server runtime; this PR only adds renderings.
- Do not put a token value, even a fake-looking one, in any shipped config.
- Do not add host-specific tools (file edit, bash, web) to the rendered agents' tool lists —
  teams opt into those themselves; we only ship the broker-facing skeleton.

## Kickoff prompt

"Implement PR-15 per the brief: contract doc first, then .opencode/ and .copilot/ trees (agents,
skills, MCP configs, READMEs), then the parity + validity + secret-scan contract tests in
services/mcp-server. Verify Copilot/OpenCode field names against the brief's researched schemas."
