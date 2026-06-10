# PR-12 — Evaluation harness

## Scope
Retrieval cases, agent-task cases, and metric reporting: duplicate-context, evidence-reuse, token-cost.

## Context
docs/architecture §13. .claude/skills/token-budget-eval. The eval-runner subagent consumes this.

## Files to create
- `evals/retrieval_cases/*.yaml`, `evals/agent_task_cases/*.yaml` (each lists expected docs, files,
  symbols, tests, open questions).
- `evals/run.py` — executes cases, computes the core metrics, writes a baseline + deltas.

## Contracts
Metric names exactly per docs/architecture §13.

## Acceptance criteria
- Runs the six benchmark task types from §13.
- Emits context_tokens_per_successful_task, duplicate_context_tokens, evidence_reuse_rate,
  semantic_cache_hit_rate, unsupported_claim_rate, missing_context_rate (+ build metrics).
- Compares against a stored baseline and prints deltas + a verdict.

## Required tests
- Metric computation correctness on a synthetic run; baseline diffing.

## Do NOT
- Modify product code to make evals pass. Real defects get filed, not masked.

## Kickoff prompt
"Implement PR-12: eval harness + metrics + baseline diffing for the six benchmark tasks. Wire it so
the eval-runner subagent can execute it."
