# PR-11 — Agent markdown manifests (product runtime)

## Scope
Author the product's runtime agent manifests in `agents/` with strict output schemas and evidence
rules: orchestrator + implementation, test_layer, code_reviewer, delivery_planner (+ pr_planner).

## Context
docs/architecture §11. Blueprint §14 (manifest + output schema examples). These are NOT Claude Code
subagents — they are consumed by the product's MCP runtime.

## Files to create / confirm
- `agents/orchestrator.md`, `agents/implementation.md`, `agents/test_layer.md`,
  `agents/code_reviewer.md`, `agents/delivery_planner.md` (templates already seeded — finalize).
- `packages/contracts/agent_output_schemas/implementation_plan_v1.py` and peers.

## Contracts
Each manifest declares allowed_tools (context.*), max_context_calls, max_context_tokens,
requires_evidence_ids, output_schema. Output schemas validate evidence_id references.

## Acceptance criteria
- Manifests match the budgets in .claude/rules/token-budgets.md.
- Output schemas require evidence IDs; a plan with an unknown evidence_id fails validation.

## Required tests
- Schema validation: every claim field carries evidence; missing evidence ⇒ open_question, not invention.

## Do NOT
- Give any subagent unrestricted KB search. Server-side policy still enforces limits.

## Kickoff prompt
"Implement PR-11: finalize product agent manifests + output schemas. Enforce evidence-ID citation in
schema validation. Match the budget rules."
