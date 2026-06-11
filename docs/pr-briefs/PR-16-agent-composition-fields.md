# PR-16 — Native subagent + skill declarations in the portable renderings

## Scope

Extend the `.copilot/` and `.opencode/` renderings (PR-15, ADR-0009) with each host's **native
composition fields**, so the files declare which subagents an agent may invoke and which framework
skills it uses — in the host's own syntax, parity-pinned like everything else. Templates align with
the full host custom-agent configuration. No canon changes, no runtime changes.

## Context

ADR-0009; docs/contracts/portable-agent-framework.md. Researched fields (June 2026 docs):

- **Copilot / VS Code custom agents** — `agents: ['name', …]` declares invocable subagents
  (`['*']` all, `[]` none); "If you specify `agents`, ensure the `agent` tool is included in the
  `tools` property." `handoffs:` (list of `label` / `agent` / `prompt` / `send`) defines workflow
  transitions — **VS Code-only** (the cloud agent supports all properties except `argument-hint`
  and `handoffs`). No native skills field — skills stay README-mapped instruction modules.
- **OpenCode** — per-agent `permission` frontmatter: `task` ("launching subagents (matches the
  subagent type)") and `skill` ("loading a skill (matches the skill name)") — pattern maps with
  `allow` / `ask` / `deny`; subagent names are agent filenames.

## Changes

- `.copilot/agents/orchestrator.agent.md` — `agents:` listing the five specialist names (canon
  `name` fields); `agent` added to `tools` (required by the `agents` field — composition, not a
  data tool); `handoffs:` to the five specialists (`send: false`), noted VS Code-only in the body.
- `.copilot/agents/<specialist>.agent.md` (five) — `agents: []` (specialists never spawn).
- `.copilot/agents/_template.agent.md` — `agents: []` default + the body explains how to widen.
- `.opencode/agents/orchestrator.md` — `permission:` with `task` allowing exactly the five
  specialist agent names (deny `"*"`), `skill` allowing `evidence-pack-orchestration` +
  `evidence-citation` (deny `"*"`).
- `.opencode/agents/<specialist>.md` (five) — `task: {"*": deny}`; `skill` allowing
  `context-request-discipline` + `evidence-citation` (deny `"*"`).
- `.opencode/agents/_template.md` — specialist-shaped permission block.
- `docs/contracts/portable-agent-framework.md` — composition section: who may invoke whom, skill
  assignment per role, the Copilot `agent`-tool exception to the tools-parity rule.
- READMEs — short "Composition" note per tree.
- `services/mcp-server/tests/contract/test_portable_agent_exports.py` — parser extended for
  nested (4-space) maps; tools-parity test updated for the orchestrator's `agent` exception.

## Acceptance criteria

- Copilot: orchestrator `agents` == the five canonical specialist names; every specialist and the
  template carry `agents: []`; orchestrator `tools` == canonical allowed_tools + `agent`; all
  other tool lists unchanged; handoff targets are valid agent names.
- OpenCode: orchestrator `permission.task` allows exactly the five specialist filenames and denies
  `"*"`; specialists deny all task launches; every `permission.skill` allow-key names a shipped
  skill; every agent denies `"*"` for skills.
- Skill assignment matches roles: orchestration skill only on the orchestrator;
  request-discipline only on agents whose canon allows `context.request_more`.
- Parity, validity, and secret-scan tests stay green; canon byte-unchanged.

## Do NOT

- Add host-native data tools (edit/bash/web) anywhere — `agent` on the orchestrator is the single
  documented exception, pinned by test.
- Change canonical manifests or opencode.json tool maps.
- Invent frontmatter fields beyond `agents`, `handoffs`, `permission.task`, `permission.skill`.
