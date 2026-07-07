# Developer guide

Documentation for installing, using, and operating the Agentic KB Platform. Pick the section
that matches what you need right now.

## Start here

**[Getting started](getting-started.md)** — the 10-minute golden path: prerequisites →
`./scripts/bootstrap.sh` → serve → your first `kb_search` answer.

## Tutorials — learn by doing

A numbered journey. Each tutorial builds on the last; do them in order.

1. [Explore what got built](tutorials/01-explore-what-got-built.md) — see your artifacts,
   aliases, and build health with `psql` and `make dashboard`.
2. [Ask your first questions](tutorials/02-ask-your-first-questions.md) — connect VS Code
   Copilot, ask real questions, watch the ledger record them.
3. [Scope a real task](tutorials/03-scope-a-real-task.md) — `get_task_context` end to end,
   field by field.
4. [Review a pull request](tutorials/04-review-a-pull-request.md) — draft a four-lens review,
   revise it, publish it under your own name.

## How-to guides — one task per page

Recipes for when you already know what you want: connect a host
([VS Code](how-to/connect-vscode.md) · [Copilot CLI](how-to/connect-copilot-cli.md) ·
[OpenCode](how-to/connect-opencode.md)), [switch LLM providers](how-to/switch-llm-providers.md),
[index your own sources](how-to/index-your-own-sources.md),
[rebuild after changes](how-to/rebuild-after-changes.md),
[back up and restore](how-to/back-up-and-restore.md), [reset the database](how-to/reset-the-database.md),
[read the dashboard](how-to/read-the-dashboard.md),
[query traces and the ledger](how-to/query-traces-and-the-ledger.md),
[tune budgets](how-to/tune-budgets.md), and [troubleshoot](how-to/troubleshoot.md).

## Reference — complete and dry

[Tools](reference/tools.md) (request/response fields, budgets, errors) ·
[Environment variables](reference/environment-variables.md) · [CLI](reference/cli.md) ·
[Database](reference/database.md) · [Agent roles](reference/agent-roles.md).

## Explanation — how and why it works

[How your knowledge base is built](explanation/how-your-knowledge-base-is-built.md) ·
[Governance and budgets](explanation/governance-and-budgets.md) ·
[The review flow](explanation/the-review-flow.md) · [Observability](explanation/observability.md).

## Contributors

Changing the platform itself, not just using it? Start at
[contributors/README.md](contributors/README.md) — architecture, decision records, code tour,
testing.

---

Cross-service contracts live in `docs/contracts/` — the source of truth when any prose
disagrees.
