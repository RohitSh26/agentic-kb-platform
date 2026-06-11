# OpenCode rendering of the agentic framework

This directory is a **host-native rendering** of the canonical agent manifests in `agents/`
(ADR-0009) for [OpenCode](https://opencode.ai). It is hand-authored and parity-pinned: contract
tests in `services/mcp-server/tests/contract/test_portable_agent_exports.py` verify that every
rendering preserves the canon's tool access, budgets, and framework rules. Do not edit the
rendered instruction bodies here — change `agents/*.md` and the tests will force this rendering
to follow. The parity checklist lives in `docs/contracts/portable-agent-framework.md`.

## Where each file goes (OpenCode discovery locations)

| File here | Where OpenCode looks for it |
|---|---|
| `agents/orchestrator.md` (mode: primary) | `.opencode/agents/orchestrator.md` in your project (or `~/.config/opencode/agents/` globally) |
| `agents/implementation.md`, `agents/test_layer.md`, `agents/code_reviewer.md`, `agents/delivery_planner.md`, `agents/pr_planner.md` (mode: subagent) | `.opencode/agents/<name>.md` in your project |
| `agents/_template.md` | copy to `.opencode/agents/<your-agent>.md`, fill in the `<!-- your agent description here -->` slots |
| `skills/evidence-pack-orchestration/SKILL.md`, `skills/context-request-discipline/SKILL.md`, `skills/evidence-citation/SKILL.md` | `.opencode/skills/<name>/SKILL.md` in your project |
| `opencode.json` | merge into your project's `opencode.json` (or `~/.config/opencode/opencode.json`) |

If you copy this whole directory to your repository root as `.opencode/`, everything is already
in its discovery location — set the broker URL and the one token and you are done.

## The one credential to set

`opencode.json` connects to the Context Broker as a remote MCP server named `context-broker`.
Replace `https://<your-broker-host>/mcp/` with your broker URL and export:

```sh
export CONTEXT_BROKER_TOKEN=<your bearer token>
```

The config references it as `{env:CONTEXT_BROKER_TOKEN}` (OpenCode's environment substitution).
**Never** write a token value into `opencode.json` or any other shipped file.

## Tool access model

`opencode.json` disables the whole broker namespace globally (`"context-broker_*": false`) and
re-enables exactly each agent's allowed tools per agent — the same lists appear in each agent
file's `tools` frontmatter map. Only the orchestrator may `context.create_pack` and read the
ledger; specialists get `context.read_pack` / `context.request_more` (and, where the canon
allows, `context.open_evidence`). No host tools (file edit, bash, web) are enabled here — your
team opts into those per agent.

## Composition

Each agent's `permission` frontmatter declares composition natively, deny-by-default: `task`
(launching subagents) and `skill` (loading skills) both deny `"*"`, then allow-list exactly what
the role needs. The orchestrator may launch the five specialists (by filename) and load
`evidence-pack-orchestration` + `evidence-citation`; specialists launch nothing and load
`context-request-discipline` + `evidence-citation`. The template ships the specialist-shaped
block.

## What the broker enforces regardless of these files

Budgets (`max_context_calls`, `max_context_tokens`), tool policy, ACL filtering, the
`context.request_more` field contract, and evidence-by-handle expansion are all enforced
**server-side by the Context Broker per authenticated identity**. These files document the
limits and teach the discipline; deleting or editing them cannot widen any agent's access or
budget.

## Rendering provenance

- Canonical manifests: `agents/*.md` version 1.0.
- OpenCode format: agent frontmatter (`description`, `mode`, `tools` glob map with MCP tools
  prefixed `<server>_`), `skills/<name>/SKILL.md` naming rules, and `opencode.json` `mcp`
  remote-server shape per the OpenCode docs as of June 2026.
