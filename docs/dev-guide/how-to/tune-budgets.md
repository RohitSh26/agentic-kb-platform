# Tune budgets

**Goal:** change what an agent may spend on knowledge retrieval — per subject, enforced
server-side.

Budgets are enforced in the tool code, on the broker. Prompts, host configs, and anything the
model sends cannot widen them.

## Steps

1. **Set the allowance** — `MCP_AGENT_ALLOWANCES` on the broker maps an authenticated subject to
   its per-session `kb_search` budget (identifiers and numbers only, never secrets):

   ```sh
   MCP_AGENT_ALLOWANCES='{"local-dev": {"max_requests": 50, "max_tokens": 50000}}'
   ```

   The subject is the verified token subject (`local-dev` in local-dev mode). An empty or unset
   variable means server defaults.

2. **Restart the broker** — allowances are read at startup. A clean boot logs
   `event=agent_allowances_loaded subjects=N`.

## What the cap does

`kb_search` carries a **dual cap** per (session, subject): a call count AND a cumulative token
total — whichever closes first. When it closes, the tool answers with empty results and exactly
this notice, rather than erroring:

> *"KB budget spent — work with what you have, or read the specific files you still need."*

That is the budget working as designed (ADR-0025): a contractual outcome, never a crash. The
agent keeps its native file tools and finishes from them. Every denial lands in the ledger as a
`denied` row. A new chat window is a new session — the cheap way out in local dev.

Two related facts:

- **`get_task_context` has its own separate budget** and its response is capped server-side at
  the Evidence-Pack band (~8k tokens), trimmed deterministically from the lowest-value tail — it
  never competes with the `kb_search` cap.
- **`get_review_draft` charges nothing** — it is a read-only fetch.

## Client scopes (optional)

`MCP_CLIENT_REGISTRY` maps a registered `client_id` to scopes and verification policy. Client
scopes gate the tool surface **additively** on top of — never replacing — the per-user team ACLs.
A deployment that ships no client registry is unaffected.

## Verify

Make one `kb_search` call, then read the budget window the ledger recorded for it:

```sh
psql agentic_kb -c "select details from retrieval_event
                    where tool_name = 'kb_search' order by created_at desc limit 1;"
```

You should see (real output):

```
{"session": "5af289b9e74d43ee99ab507d81758862", "calls_used": 1, "max_tokens": 50000,
 "tokens_used": 550, "max_requests": 50}
```

`max_requests`/`max_tokens` reflect your allowance; `calls_used`/`tokens_used` count down the
session. Budget breaches surface per (run, agent) in `v_budget_adherence`
([read the dashboard](read-the-dashboard.md)). The run/agent band numbers for the governed
evidence-pack flow live in `.claude/rules/token-budgets.md` and are asserted in tests. Why
budgets are structural, not advisory: [governance and budgets](../explanation/governance-and-budgets.md).
