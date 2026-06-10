# Product runtime agent manifests

These are the **product's** agents — the orchestrator and subagents the finished MCP runtime serves to
developers. They are NOT Claude Code subagents (those live in `.claude/agents/`).

Format follows the blueprint's manifest style: YAML-ish frontmatter declaring tool access and budgets,
plus a body that is the agent's instruction set. Server-side MCP policy enforces every limit even if a
prompt fails. Output schemas live in `packages/contracts/agent_output_schemas/`.

Budgets here must match `.claude/rules/token-budgets.md`.
