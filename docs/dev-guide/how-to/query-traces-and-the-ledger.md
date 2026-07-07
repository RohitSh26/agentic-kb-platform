# Query traces and the ledger

**Goal:** answer "what did an agent do, step by step, and what did it cost?" straight from
Postgres.

Two tables hold the answers. `retrieval_event` is the **ledger**: one row per broker tool call,
complete by construction — a crashed call still lands exactly once, and any budget charge made
before a failure is refunded. `trace_span` is the **trace store**: one row per completed step
inside a call, aggregate-only.

## The ledger: `retrieval_event`

What the `status` column means:

| Status | Meaning |
|---|---|
| `approved` | new evidence retrieved and charged against the budget |
| `reused` | the question matched a previous retrieval — returned at no budget cost |
| `denied` | a budget said no. A denial is a **contractual outcome, not an error** — the tool returns the "work with what you have" notice, never a crash |
| `needs_human_approval` | a justified request that would exceed the remaining per-run budget |
| `error` | the call failed. Unresolvable `run_id`/`kb_version` values are recorded as the sentinel `"-"` |

The last 20 calls, newest first:

```sql
SELECT tool_name, agent_name, status, tokens_returned, created_at
FROM retrieval_event ORDER BY created_at DESC LIMIT 20;
```

How close a session is to its `kb_search` budget — the `details` JSONB column carries the budget
window after each call:

```sql
SELECT created_at, details
FROM retrieval_event
WHERE tool_name = 'kb_search'
ORDER BY created_at DESC LIMIT 5;
```

You should see (real output):

```
{"session": "5af289b9e74d43ee99ab507d81758862", "calls_used": 1, "max_tokens": 50000,
 "tokens_used": 550, "max_requests": 50}
```

(`get_task_context` rows carry a per-node latency breakdown in the same column.)

Denials and errors — both first-class outcomes, never silent:

```sql
SELECT tool_name, agent_name, status, created_at
FROM retrieval_event WHERE status IN ('denied', 'error') ORDER BY created_at DESC LIMIT 20;
```

The zero-thin KB-gap rows — answered `kb_search` calls that returned ≤ 1 artifact (the daily rate
is on the dashboard; these are the underlying rows):

```sql
SELECT tool_name, agent_name, coalesce(cardinality(returned_artifact_ids), 0) AS n_results, created_at
FROM retrieval_event
WHERE tool_name = 'kb_search' AND status = 'approved'
  AND coalesce(cardinality(returned_artifact_ids), 0) <= 1
ORDER BY created_at DESC LIMIT 20;
```

Zero rows back is a *good* sign — no gaps in that window. Each build also mines these misses into
alias entries so the exact phrase resolves next time; the dashboard's mined-vs-unresolved split
tracks it ([read the dashboard](read-the-dashboard.md)).

Tokens by agent and day:

```sql
SELECT date_trunc('day', created_at)::date AS day,
       agent_name,
       count(*)                          AS calls,
       sum(coalesce(tokens_returned, 0)) AS tokens
FROM retrieval_event
GROUP BY 1, 2
ORDER BY 1 DESC, 4 DESC;
```

## The traces: `trace_span`

One row = one completed unit of work: `span_id`, `trace_id` (groups a call's spans), nullable
`parent_span_id` (NULL = root), `name`, `service`, `started_at`/`ended_at`, `status`
(`ok | error`), and aggregate-only `attributes` — the span constructor rejects content-shaped
keys, so a prompt or query can never land here. Tracing is fail-soft: a dead or slow sink never
fails, delays, or budget-charges the call it observes.

Selection is by env var, per service:

| `TRACE_SINK` | Effect |
|---|---|
| unset / `postgres` | write spans to Postgres when a database is configured, else a no-op sink |
| `none` | no-op sink unconditionally |
| anything else | configuration error — fails at startup, never silently |

Recent traces (root spans first):

```sql
SELECT trace_id, name, status, started_at
FROM trace_span
WHERE parent_span_id IS NULL
ORDER BY started_at DESC
LIMIT 10;
```

You should see rows like (real output):

```
               trace_id               |       name       | status |          started_at
--------------------------------------+------------------+--------+-------------------------------
 0f42ba2a-1735-4855-8641-1b95a8d0a3f5 | get_task_context | ok     | 2026-07-07 15:43:07.946525-05
 2d4cc75a-bbe8-4516-8d50-a34b0a0e6ab1 | get_task_context | ok     | 2026-07-07 15:43:07.253564-05
```

The slowest node — where does `get_task_context` spend its time?

```sql
SELECT name,
       count(*)                                                          AS spans,
       round(avg(extract(epoch FROM ended_at - started_at)) * 1000)      AS avg_ms,
       round(max(extract(epoch FROM ended_at - started_at)) * 1000)      AS max_ms
FROM trace_span
WHERE started_at > now() - interval '7 days'
GROUP BY name
ORDER BY avg_ms DESC;
```

You should see (real output):

```
         name          | spans | avg_ms | max_ms
-----------------------+-------+--------+--------
 get_task_context      |    60 |   1211 |   2636
 conventions           |    60 |   1114 |   2207
 resolve_scope         |    60 |    910 |   2629
 blast_radius          |    60 |    890 |   2381
 similar_prior_changes |    60 |    842 |   1376
 kb_search             |    77 |    652 |   1075
 synthesize            |    60 |      0 |      0
```

Every span of one trace — the step-by-step timeline of a single call:

```sql
SELECT name, status,
       round(extract(epoch FROM ended_at - started_at) * 1000) AS ms,
       started_at
FROM trace_span
WHERE trace_id = '<trace-id-from-the-query-above>'
ORDER BY started_at;
```

**Review-panel runs** trace into their own schema — run the same span queries against
`review_panel.trace_span`. Its root span is `review_panel.draft_run`; its nodes are `load_pr`,
`review_bug`, `review_security`, `review_quality`, `review_test_coverage`, `reconcile`,
`store_draft`. The `trace_id` is the draft key, so a crash + resume correlate under one trace.
Span shape: [the tracing contract](../../contracts/tracing.md).

## Maintenance

`trace_span` is pure derived observability — safe to prune by age. Preview, then delete:

```sql
-- dry run: how many would go?
SELECT count(*) FROM trace_span WHERE started_at < now() - interval '90 days';

-- the actual prune
DELETE FROM trace_span WHERE started_at < now() - interval '90 days';
```

Run `VACUUM ANALYZE trace_span;` after a large bulk delete so the planner's statistics catch up
immediately. `retrieval_event` is the **audit record** — never prune it without an explicit
retention decision, and never hand-edit the `knowledge_*` tables outside the build engine.
