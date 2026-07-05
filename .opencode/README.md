# OpenCode rendering of the agentic framework

This directory is a **host-native rendering** of the canonical agent manifests in `agents/`
(ADR-0009) for [OpenCode](https://opencode.ai). It is hand-authored and parity-pinned: contract
tests in `services/mcp-server/tests/contract/test_portable_agent_exports.py` verify that every
rendering preserves the canon's tool access, budgets, and framework rules. Do not edit the
rendered instruction bodies here — change `agents/*.md` and the tests will force this rendering
to follow. The parity checklist lives in `docs/contracts/portable-agent-framework.md`.

You can add your own agents next to the framework's twelve (canon manifest in `agents/`, a
rendering here, an `opencode.json` entry) — run `python agents/check_parity.py` to verify your
whole tree stays parity-clean without this repo's test suite (stdlib-only, CI-friendly).

## Where each file goes (OpenCode discovery locations)

| File here | Where OpenCode looks for it |
|---|---|
| `agents/orchestrator.md` (mode: primary) | `.opencode/agents/orchestrator.md` in your project (or `~/.config/opencode/agents/` globally) |
| `agents/implementation.md`, `agents/test_layer.md`, `agents/code_reviewer.md`, `agents/delivery_planner.md`, `agents/pr_planner.md`, `agents/adr_writer.md`, `agents/infra_code.md` (mode: subagent — all seven are on the orchestrator's `task` allowlist, the last two added by ADR-0030) | `.opencode/agents/<name>.md` in your project |
| `agents/bug_reviewer.md`, `agents/security_reviewer.md`, `agents/quality_reviewer.md`, `agents/test_coverage_reviewer.md` (mode: subagent, ADR-0030 review-panel lenses — NOT on the orchestrator's allowlist; they run only via the ADR-0031 dev-gated review-panel draft engine, never launched in-session) | `.opencode/agents/<name>.md` in your project |
| `agents/_template.md` | copy to `.opencode/agents/<your-agent>.md`, fill in the `<!-- your agent description here -->` slots |
| `skills/evidence-pack-orchestration/SKILL.md`, `skills/context-request-discipline/SKILL.md`, `skills/evidence-citation/SKILL.md` | `.opencode/skills/<name>/SKILL.md` in your project |
| `opencode.json` | merge into your project's `opencode.json` (or `~/.config/opencode/opencode.json`) |

If you copy this whole directory to your repository root as `.opencode/`, everything is already
in its discovery location — set the broker URL and the one token and you are done.

## The one credential to set

`opencode.json` connects to the Context Broker as a remote MCP server named `context-broker`
(ADR-0025/ADR-0030: the broker now serves exactly the two MCP tools the twelve-role canon
actually grants — the budgeted `kb_search` and the one-call `get_task_context` — never the full
broker surface). Replace `https://<your-broker-host>/mcp/` with your broker URL and export:

```sh
export CONTEXT_BROKER_TOKEN=<your bearer token>
```

The config references it as `{env:CONTEXT_BROKER_TOKEN}` (OpenCode's environment substitution).
**Never** write a token value into `opencode.json` or any other shipped file.

## Tool access model

Each agent grants exactly its canonical `allowed_tools`, mapped through two different rules
(`docs/contracts/portable-agent-framework.md` has the full table):

- **`kb_search`** and **`get_task_context`** are the two MCP tools — both budgeted and
  server-enforced, independently. `opencode.json` disables the whole broker namespace globally
  (`"context-broker_*": false`) and re-enables exactly what each agent's canon grants. `kb_search`
  is granted to every role; `get_task_context` (ADR-0030's one-call task-context tool) is granted
  only to the task-scoped BUILD-lane roles — `orchestrator`, `implementation`, `infra_code`,
  `test_layer` — not to the review/synthesis/planning-only roles.
- **Native tools** (`read`, `edit`, `grep`, `list` — OpenCode's own built-ins, mapped from the
  canon's `read_file`/`read_full`/`edit_file`/`grep`/`list_files`) are restored directly to the
  agent (ADR-0025: "native tools are never removed") and carry **no broker budget**.
  `opencode.json` denies each of these four tool names globally too, then re-enables per agent
  exactly what its canon grants — so an agent with no `edit_file` in its canon (e.g. the
  planners) genuinely cannot edit, not just "isn't told to."

No other host tools (`bash`, `write`, `webfetch`, …) are enabled here — your team opts into those
per agent; no framework role needs them today.

## Composition

Each agent's `permission` frontmatter declares composition natively, deny-by-default: `task`
(launching subagents) and `skill` (loading skills) both deny `"*"`, then allow-list exactly what
the role needs. Only the agent whose canon sets `requires_human_approval: true` (today, only the
orchestrator) may launch subagents — it launches all seven build-lane specialists (by filename):
`implementation`, `test_layer`, `code_reviewer`, `delivery_planner`, `pr_planner`, `adr_writer`,
`infra_code`. The four review-panel lenses are deliberately absent from this list — they are
reachable only through the separate ADR-0031 review-panel draft engine, not this orchestrator.
Every role, orchestrator included, loads the same two framework skills: `kb-first-file-fallback` +
`evidence-citation` (ADR-0025 dropped the old orchestrator-only `evidence-pack-orchestration`
skill — its short procedure now lives directly in the orchestrator's own canonical body). The
template ships the specialist-shaped block.

## What the broker enforces regardless of these files

The `kb_search` budget (`max_context_calls`, `max_context_tokens`) and ACL filtering are enforced
**server-side by the Context Broker per authenticated identity**. These files document the
limits and teach the discipline; deleting or editing them cannot widen any agent's access or
budget. Native tools carry no broker budget — they are gated only by what each host's own config
grants (see Tool access model above).

## Rendering provenance

- Canonical manifests: `agents/*.md`, version stated in each rendered body's own provenance
  comment (`<!-- rendered from agents/<role>.md v<N> -->`) — versions differ per role as the
  canon evolves; there is no single blanket version for the whole set.
- OpenCode format: agent frontmatter (`description`, `mode`, `tools` glob map — MCP tools
  prefixed `<server>_`, native tools by their own built-in id), `permission` (`task`/`skill`
  deny-by-default), `skills/<name>/SKILL.md` naming rules, and `opencode.json` `mcp` remote-server
  shape per the OpenCode docs as of July 2026.
