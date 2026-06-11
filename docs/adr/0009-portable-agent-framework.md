# ADR-0009 — Portable agent framework: host-native renderings of the agent manifests

## Status

Accepted (2026-06-11)

## Context

The product's value is the **framework** — orchestration over one shared Evidence Pack, disciplined
context passing (`context.request_more`), and server-enforced token budgets — not the six specific
agents in `agents/`. Teams adopting the platform run agents in different hosts: GitHub Copilot
(custom agents in `.github/agents/*.agent.md`, MCP via repository settings or `.vscode/mcp.json`)
and OpenCode (`.opencode/agents/*.md`, `.opencode/skills/*/SKILL.md`, MCP via `opencode.json`).
Each host has its own file format; the *content* — broker tool access, evidence-citation rules,
request-more discipline, untrusted-content rule — is the same.

Two properties of the existing architecture make portability cheap and safe:

1. **Invariant 3/6: enforcement is server-side.** Budgets, tool policy, ACLs, and the
   request-more contract are enforced by the Context Broker per authenticated subject. A host
   format that cannot express `max_context_tokens` loses only the *documentation* of the limit,
   never the limit itself.
2. **The manifests are already content + metadata.** `agents/*.md` frontmatter (allowed_tools,
   budgets, requires_evidence_ids, output_schema) plus an instruction body maps onto any host's
   agent-file shape.

## Decision

- `agents/*.md` remains the **canonical** manifest set — the single source of truth for tool
  access, budgets, and instruction content.
- Ship **host-native renderings** under top-level `.copilot/` and `.opencode/` directories, each
  containing `agents/` (the six manifests rendered to the host's frontmatter, plus a `_template`
  with a clearly marked description slot teams fill in) and `skills/` (the framework procedures —
  orchestration, context-request discipline, evidence citation — as reusable instruction modules),
  plus the host's MCP connection config pointing at the Context Broker and a README mapping each
  file to the host's discovery location (`.github/agents/`, `~/.copilot/agents/`,
  `opencode.json`).
- Renderings are **hand-authored, parity-pinned**: contract tests in `services/mcp-server`
  (which already pins `agents/` against budgets and the output-schema registry) parse the
  canonical frontmatter and assert each rendering preserves the framework semantics — every
  allowed tool present, budgets stated, evidence-ID rule, request-more field discipline, and the
  untrusted-content rule. No generator code in V1; a generator becomes worthwhile only if the
  manifest count grows.
- **Secrets stay by reference** in every rendering: Copilot configs use `$COPILOT_MCP_*` secret
  references or `${input:...}` prompts; OpenCode uses environment substitution. A token value is
  never written into any shipped file.

## Consequences

- Teams on Copilot or OpenCode adopt the framework by copying a directory and setting one secret;
  their agent *descriptions* are free-form, while the framework skeleton (broker tools, evidence
  discipline) arrives intact and is verified by tests.
- Anything inexpressible in a host format is folded into the instruction body as stated policy and
  remains enforced server-side — drift between hosts cannot weaken guarantees.
- Two more places state framework rules; the parity contract tests are what keep them honest.
- Host formats evolve (Copilot's custom-agent surface is changing quickly); the renderings carry a
  `version` line and the README records the doc revisions they were written against.
