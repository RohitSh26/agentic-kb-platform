# GitHub Copilot rendering of the agentic framework

This directory is a **host-native rendering** of the canonical agent manifests in `agents/`
(ADR-0009) for GitHub Copilot custom agents. It is hand-authored and parity-pinned: contract
tests in `services/mcp-server/tests/contract/test_portable_agent_exports.py` verify that every
rendering preserves the canon's tool access, budgets, and framework rules. Do not edit the
rendered instruction bodies here — change `agents/*.md` and the tests will force this rendering
to follow. The parity checklist lives in `docs/contracts/portable-agent-framework.md`.

You can add your own agents next to the framework's twelve (canon manifest in `agents/`, a
`*.agent.md` rendering here) — run `python agents/check_parity.py` to verify your whole tree
stays parity-clean without this repo's test suite (stdlib-only, CI-friendly).

## Where each file goes (Copilot discovery locations)

| File here | Where Copilot looks for it |
|---|---|
| `agents/orchestrator.agent.md` and the eleven specialist `*.agent.md` files (five original plus six added by ADR-0030, not yet invoked by the orchestrator) | `.github/agents/` in your repository (used by VS Code and the cloud agent), or `~/.copilot/agents/` for a user-level Copilot CLI profile |
| `agents/_template.agent.md` | copy to `.github/agents/<your-agent>.agent.md`, fill in the `<!-- your agent description here -->` slots and the `name` |
| `skills/evidence-pack-orchestration.md`, `skills/context-request-discipline.md`, `skills/evidence-citation.md` | host-neutral instruction modules — append the relevant one(s) to an agent body, or add them to your repository custom instructions (`.github/copilot-instructions.md`) |
| `mcp/repository-settings.json` | repository settings → Copilot → MCP servers (the cloud agent / Copilot CLI configuration JSON) |
| `mcp/vscode-mcp.json` | `.vscode/mcp.json` in your repository (VS Code MCP configuration) |

The agent frontmatter carries no `mcp-servers` block on purpose: VS Code ignores it, so the
broker connection ships separately in `mcp/` for both deployment shapes.

## The one credential to set

Both configs connect to the Context Broker as an MCP server named `context-broker` (ADR-0025:
the broker now serves exactly one tool, the budgeted `kb_search`). Replace
`https://<your-broker-host>/mcp/` with your broker URL, then:

- **Repository settings (cloud agent / CLI)**: create the Copilot environment value
  `COPILOT_MCP_CONTEXT_BROKER_TOKEN` (Copilot only exposes values whose names start with
  `COPILOT_MCP_`); `repository-settings.json` references it as
  `$COPILOT_MCP_CONTEXT_BROKER_TOKEN`.
- **VS Code**: `vscode-mcp.json` declares a `password` input with id `context-broker-token`;
  VS Code prompts for the token on first use via `${input:context-broker-token}`.

**Never** write a token value into these files.

## Tool access model

Each `*.agent.md` lists exactly its canonical `allowed_tools`, mapped through two different
rules (`docs/contracts/portable-agent-framework.md` has the full table):

- **`kb_search`** is the one MCP tool — budgeted and server-enforced — in Copilot's MCP syntax
  `context-broker/kb_search`.
- **Native tools** (`read`, `edit`, `search` — Copilot's own built-in aliases, mapped from the
  canon's `read_file`/`read_full`/`edit_file`/`grep`/`list_files`; `search` covers both grep and
  glob) are restored directly to the agent (ADR-0025: "native tools are never removed") and
  carry **no broker budget**. An agent with no `edit_file` in its canon (e.g. the planners)
  simply has no `edit` in its `tools` list.

No other host tools (shell, web) are enabled here — your team opts into those per agent; no
framework role needs them today.

## Composition

The orchestrator declares its five invocable specialists natively — `agents: [...]` plus
matching `handoffs:` (VS Code-only; the cloud agent ignores handoffs) — and therefore carries
the `agent` tool alongside its mapped tools, the one pinned exception to the tool-parity rule.
Every specialist and the template declare `agents: []`: specialists never launch subagents. Only
the agent whose canon sets `requires_human_approval: true` (today, only the orchestrator) may
declare subagents at all. Copilot has no native skills field, so the `skills/` modules stay wired
per the table above — every agent, orchestrator included, is meant to carry the same two:
`kb-first-file-fallback` + `evidence-citation` (ADR-0025 dropped the old orchestrator-only
`evidence-pack-orchestration` skill; its short procedure now lives directly in the orchestrator's
own canonical body).

## What the broker enforces regardless of these files

The `kb_search` budget (`max_context_calls`, `max_context_tokens`) and ACL filtering are enforced
**server-side by the Context Broker per authenticated identity**. These files document the
limits and teach the discipline; deleting or editing them cannot widen any agent's access or
budget. Native tools carry no broker budget — they are gated only by what each agent's `tools`
list grants (see Tool access model above).

## Rendering provenance

- Canonical manifests: `agents/*.md`, version stated in each rendered body's own provenance
  comment (`<!-- rendered from agents/<role>.md v<N> -->`) — versions differ per role as the
  canon evolves; there is no single blanket version for the whole set.
- Copilot format: `*.agent.md` frontmatter (`name`, `description`, `tools` with MCP syntax
  `server-name/tool-name` for broker tools, bare aliases for native tools; body ≤ 30,000 chars),
  repository-settings `mcpServers` shape, and `.vscode/mcp.json` `servers`/`inputs` shape per the
  GitHub Copilot docs as of July 2026.
