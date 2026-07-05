# Product runtime agent manifests

These are the **product's** agents — the orchestrator and subagents the finished MCP runtime serves to
developers. They are NOT Claude Code subagents (those live in `.claude/agents/`).

Format follows the blueprint's manifest style: YAML frontmatter declaring tool access
(`allowed_tools` — the budgeted `kb_search` tool, granted to every role, plus `get_task_context`
(ADR-0030's one-call task-context tool, its own separately-budgeted grant reserved for the
task-scoped BUILD-lane roles: `orchestrator`, `implementation_agent`, `infra_code_agent`,
`test_layer_agent`), plus whichever native tools the role needs (`read_file`, `read_full`,
`list_files`, `grep`, `edit_file`); never unrestricted KB search — see ADR-0025), budgets
(`max_context_calls`, `max_context_tokens`, counted against `kb_search` calls/tokens only —
`get_task_context` is capped separately, server-side, at the Evidence-Pack band — not the retired
broker flow), `requires_evidence_ids`, and an `output_schema` name, plus a body that is the agent's
instruction set. Both `kb_search` and `get_task_context` enforce their own budgets server-side even
if a prompt fails. Output rules live in `docs/contracts/agent-output-contracts.md`;
the concrete schemas (and the `AGENT_OUTPUT_SCHEMAS` registry the `output_schema` names resolve
against) live in `services/mcp-server/src/agentic_mcp_server/agent_output_schemas/`.

All twelve manifests in this directory are now **rendered** into `.opencode/`/`.copilot/` and held
to the same checklist by `check_parity.py`: the original six — `orchestrator`,
`implementation_agent`, `test_layer_agent`, `delivery_planner_agent`, `pr_planner_agent`,
`code_reviewer_agent` (the ADR-0025/ADR-0009 rewrite) — plus six more authorized by ADR-0030 —
`adr_writer_agent`, `infra_code_agent`, and a four-agent review panel (`bug_reviewer_agent`,
`security_reviewer_agent`, `quality_reviewer_agent`, `test_coverage_reviewer_agent`, whose findings
`code_reviewer_agent` reconciles as a synthesizer). ADR-0030 resolved the roster question raised by
`docs/proposals/2026-07-02-v2-world-class-platform-architecture.md` (that proposal stays a proposal
document; ADR-0030 is what makes the roster decision durable). `agents/orchestrator.md` reaches
`adr_writer_agent` and `infra_code_agent` as BUILD-lane specialists; the four panel reviewers are
deliberately NOT on its allowlists — per ADR-0031 they run on-demand from a developer's own session
(`scripts/run_review_panel_local.sh` / `uv run review-panel draft`; a LangGraph fan-out reconciled
by `code_reviewer_agent`), always producing a stored draft the developer reads, revises, and
publishes themselves — there is no GitHub Actions review workflow and the panel never posts on its
own — but they are still never launched in-session by the framework orchestrator.

Budgets here must match `.claude/rules/token-budgets.md`; mcp-server's contract tests check the
manifests against both the budget rules and the schema registry.

> **Renderings**: the repo-root `.copilot/` and `.opencode/` trees are host-native *renderings*
> of this canon for GitHub Copilot and OpenCode (ADR-0009). This directory stays the single
> source of truth; the renderings are hand-authored and parity-pinned by
> `services/mcp-server/tests/contract/test_portable_agent_exports.py` (checklist:
> `docs/contracts/portable-agent-framework.md`). Change a manifest here and the contract tests
> force the renderings to follow in the same PR.

The pinning is **minimum + whatever exists**: the framework's six pinned roles must stay, and any
agent a team adds next to them (the six proposal roles above included) is held to the same parity
checklist. `check_parity.py` in this
directory is the standalone, stdlib-only version of that checklist for adopters who copy
`agents/` + `.copilot/` + `.opencode/` without this repo's test suite:

```sh
python agents/check_parity.py   # exit 0 = parity-clean, exit 1 = one line per problem
```
