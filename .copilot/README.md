# GitHub Copilot rendering of the agentic framework

This directory is a **host-native rendering** of the canonical agent manifests in `agents/`
(ADR-0009) for GitHub Copilot custom agents. It is hand-authored and parity-pinned: contract
tests in `services/mcp-server/tests/contract/test_portable_agent_exports.py` verify that every
rendering preserves the canon's tool access, budgets, and framework rules. Do not edit the
rendered instruction bodies here — change `agents/*.md` and the tests will force this rendering
to follow. The parity checklist lives in `docs/contracts/portable-agent-framework.md`.

You can add your own agents next to the framework's six (canon manifest in `agents/`, a
`*.agent.md` rendering here) — run `python agents/check_parity.py` to verify your whole tree
stays parity-clean without this repo's test suite (stdlib-only, CI-friendly).

## Where each file goes (Copilot discovery locations)

| File here | Where Copilot looks for it |
|---|---|
| `agents/orchestrator.agent.md` and the five specialist `*.agent.md` files | `.github/agents/` in your repository (used by VS Code and the cloud agent), or `~/.copilot/agents/` for a user-level Copilot CLI profile |
| `agents/_template.agent.md` | copy to `.github/agents/<your-agent>.agent.md`, fill in the `<!-- your agent description here -->` slots and the `name` |
| `skills/evidence-pack-orchestration.md`, `skills/context-request-discipline.md`, `skills/evidence-citation.md` | host-neutral instruction modules — append the relevant one(s) to an agent body, or add them to your repository custom instructions (`.github/copilot-instructions.md`) |
| `mcp/repository-settings.json` | repository settings → Copilot → MCP servers (the cloud agent / Copilot CLI configuration JSON) |
| `mcp/vscode-mcp.json` | `.vscode/mcp.json` in your repository (VS Code MCP configuration) |

The agent frontmatter carries no `mcp-servers` block on purpose: VS Code ignores it, so the
broker connection ships separately in `mcp/` for both deployment shapes.

## The one credential to set

Both configs connect to the Context Broker as an MCP server named `context-broker`. Replace
`https://<your-broker-host>/mcp/` with your broker URL, then:

- **Repository settings (cloud agent / CLI)**: create the Copilot environment value
  `COPILOT_MCP_CONTEXT_BROKER_TOKEN` (Copilot only exposes values whose names start with
  `COPILOT_MCP_`); `repository-settings.json` references it as
  `$COPILOT_MCP_CONTEXT_BROKER_TOKEN`.
- **VS Code**: `vscode-mcp.json` declares a `password` input with id `context-broker-token`;
  VS Code prompts for the token on first use via `${input:context-broker-token}`.

**Never** write a token value into these files.

## Tool access model

Each `*.agent.md` lists exactly its canonical `allowed_tools` in Copilot's MCP tool syntax
(`context-broker/<tool>`). Only the orchestrator may `context-broker/context.create_pack` and
read the ledger; specialists get `read_pack` / `request_more` (and, where the canon allows,
`open_evidence`). No host tools (file edit, shell, web) are enabled here — your team opts into
those per agent.

## Composition

The orchestrator declares its five invocable specialists natively — `agents: [...]` plus
matching `handoffs:` (VS Code-only; the cloud agent ignores handoffs) — and therefore carries
the `agent` tool alongside its broker tools, the one pinned exception to the broker-only tool
lists. Every specialist and the template declare `agents: []`: specialists never launch
subagents. Copilot has no native skills field, so the `skills/` modules stay wired per the
table above.

## What the broker enforces regardless of these files

Budgets (`max_context_calls`, `max_context_tokens`), tool policy, ACL filtering, the
`context.request_more` field contract, and evidence-by-handle expansion are all enforced
**server-side by the Context Broker per authenticated identity**. These files document the
limits and teach the discipline; deleting or editing them cannot widen any agent's access or
budget.

## Rendering provenance

- Canonical manifests: `agents/*.md` version 1.0.
- Copilot format: `*.agent.md` frontmatter (`name`, `description`, `tools` with MCP syntax
  `server-name/tool-name`; body ≤ 30,000 chars), repository-settings `mcpServers` shape, and
  `.vscode/mcp.json` `servers`/`inputs` shape per the GitHub Copilot docs as of June 2026.
