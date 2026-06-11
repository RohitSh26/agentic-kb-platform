# Product runtime agent manifests

These are the **product's** agents — the orchestrator and subagents the finished MCP runtime serves to
developers. They are NOT Claude Code subagents (those live in `.claude/agents/`).

Format follows the blueprint's manifest style: YAML frontmatter declaring tool access
(`allowed_tools` — context.\*/ledger.\* only, never unrestricted KB search), budgets
(`max_context_calls`, `max_context_tokens`), `requires_evidence_ids`, and an `output_schema` name,
plus a body that is the agent's instruction set. Server-side MCP policy enforces every limit even if
a prompt fails. Output rules live in `docs/contracts/agent-output-contracts.md`; the concrete
schemas (and the `AGENT_OUTPUT_SCHEMAS` registry the `output_schema` names resolve against) live in
`services/mcp-server/src/agentic_mcp_server/agent_output_schemas/`.

Budgets here must match `.claude/rules/token-budgets.md`; mcp-server's contract tests check the
manifests against both the budget rules and the schema registry.
