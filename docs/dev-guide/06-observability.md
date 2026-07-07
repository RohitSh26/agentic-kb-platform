# 06 — Observability

How to see what the platform did — retrieval outcomes, token spend, build health, per-step
latency — from the records the system writes about itself. For an operator or developer asking
"what happened, and what did it cost?". Everything here is read-only over Postgres: the ledger
(`retrieval_event`), the build audit (`kb_build_run`), the trace store (`trace_span`), and four
views projected over them; contracts:
[`observability-dashboard.md`](../contracts/observability-dashboard.md) and
[`tracing.md`](../contracts/tracing.md).

## The one-command view: `make dashboard`

```sh
# DATABASE_URL should point at a real registry (built KB); falls back to TEST_DATABASE_URL
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" make dashboard
```

This runs the read-only renderer (`evals/harness/dashboard.py` via `evals/run.py --dashboard`) and
writes `dashboard.html` (self-contained, no external assets) and `dashboard.md` into `evals/`
(override with `--dashboard-out` when invoking `run.py` directly). It only ever issues `SELECT`s
against the four `v_*` views below — plus, when `evals/report.json` exists, the latest golden-eval
block (mean/min evidence recall vs the 0.95 floor, ACL leaks). It is **aggregate-only**: statuses,
counts, token totals, and evidence *id arrays* — never query text, never artifact bodies, never
raw evidence content.

What each tile answers:

| Tile (view) | The question it answers |
|---|---|
| **Retrieval health** (`v_retrieval_health`) | Is retrieval healthy, day by day? Statuses per day, `error_rate`, `evidence_reuse_rate`, `semantic_cache_hit_rate`, and the **KB-gap proxy**: `kb_search_zero_thin_rate` (answered `kb_search` calls that returned ≤ 1 artifact — the ledger's best visible signal that agents are asking for knowledge the KB doesn't have). |
| **Token economics** (`v_token_economics`) | What is context costing? `tokens_charged` per day, `context_tokens_per_run`, `retrieval_calls_per_agent`. |
| **Build health** (`v_build_health`) | Did the builds behave? One row per `kb_build_run`: status, duration, `llm_calls_per_changed_source` / `embedding_calls_per_changed_source` (the incremental-build cache-efficiency signal), `failed_gate`, and `active_kb_age_seconds` on the single active row. |
| **Budget adherence** (`v_budget_adherence`) | Is any agent over its budget? Per (`run_id`, `agent_name`): tokens charged, follow-up requests/tokens, and `over_run_budget` / `over_agent_requests` / `over_agent_tokens` flags against the numbers in `.claude/rules/token-budgets.md`. |

## The four views

The views live in the Knowledge Registry, created by kb-builder migration `0020_dashboard_views`.
They are pure projections over `retrieval_event` and `kb_build_run` — dropping them loses nothing
— and column names that match `evals/harness/metrics.py` share the *definition* (a gate and its
dashboard tile can never disagree; ADR-0014). Query them directly whenever the rendered dashboard
is too coarse:

```sh
psql agentic_kb -c "SELECT * FROM v_retrieval_health ORDER BY day DESC LIMIT 7;"
psql agentic_kb -c "SELECT * FROM v_build_health ORDER BY build_seq DESC LIMIT 5;"
psql agentic_kb -c "SELECT * FROM v_budget_adherence WHERE over_run_budget OR over_agent_tokens;"
```

Full column-by-column definitions:
[`docs/contracts/observability-dashboard.md`](../contracts/observability-dashboard.md).

## Per-step traces: `trace_span` (ADR-0032)

Per-step tracing is **Postgres rows behind a `TraceSink` port — no hosted tracing SaaS** (ADR-0032
withdrew the earlier LangSmith commitment before it ever shipped; `LANGSMITH_*` env vars are
inert). Two graphs trace themselves, each into its own service's table:

| What's traced | Table | Span names |
|---|---|---|
| `get_task_context` (+ `kb_search`) in mcp-server | `trace_span` (Knowledge Registry public schema, migration `0021_trace_span`) | root `get_task_context`; nodes `resolve_scope`, `blast_radius`, `conventions`, `similar_prior_changes`, `synthesize`, `broaden`; `kb_search` is its own single root span |
| The review-panel draft engine | `review_panel.trace_span` (the service's own schema) | root `review_panel.draft_run`; nodes `load_pr`, `review_bug`/`review_security`/`review_quality`/`review_test_coverage`, `reconcile`, `store_draft` |

One row = one completed unit of work: `span_id`, `trace_id` (groups a call's spans; mcp-server
mints one per tool call, review-panel uses the draft key so crash + resume correlate), nullable
`parent_span_id` (NULL = root), `name`, `service`, `started_at`/`ended_at`, `status`
(`ok | error`), and aggregate-only `attributes` (counts, booleans, token totals — the span
constructor *rejects* content-shaped keys, so a prompt or query can never land here).

Tracing is **fail-soft by contract**: a dead or slow sink never fails, delays, or budget-charges
the call it observes — the span is emitted after the call's own work is already done, and a sink
error is logged (`event=trace_sink_error`) and dropped.

Selection is by env var, per service:

| `TRACE_SINK` | Effect |
|---|---|
| unset / `postgres` | write spans to Postgres when a database is configured, else a no-op sink |
| `none` | no-op sink unconditionally |
| anything else | configuration error — fails at startup, never silently |

### Copy-paste queries

Find recent traces (root spans first):

```sql
SELECT trace_id, name, status, started_at
FROM trace_span
WHERE parent_span_id IS NULL
ORDER BY started_at DESC
LIMIT 10;
```

**Slowest node** — where does `get_task_context` spend its time?

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

**Every span of one trace** — the step-by-step timeline of a single call:

```sql
SELECT name, status,
       round(extract(epoch FROM ended_at - started_at) * 1000) AS ms,
       started_at
FROM trace_span
WHERE trace_id = '<trace-id-from-the-query-above>'
ORDER BY started_at;
```

**Tokens by agent and day** — this one reads the retrieval ledger, not the trace store:

```sql
SELECT date_trunc('day', created_at)::date        AS day,
       agent_name,
       count(*)                                   AS calls,
       sum(coalesce(tokens_returned, 0))          AS tokens
FROM retrieval_event
GROUP BY 1, 2
ORDER BY 1 DESC, 4 DESC;
```

(For review-panel runs, run the same span queries against `review_panel.trace_span`.)

## Reading the retrieval ledger

`retrieval_event` is the durable record of every broker tool call — **complete by construction,
including crashes**. One row per call, always. What the `status` column means:

| Status | Meaning |
|---|---|
| `approved` | new evidence retrieved and charged against the budget |
| `reused` | the question matched a previous retrieval — existing evidence returned at no budget cost |
| `denied` | a budget said no: the per-agent allowance is exhausted, or `kb_search`'s dual call+token cap closed. A denial is a **contractual outcome, not an error** — the tool returns the "work with what you have" notice, never a crash |
| `needs_human_approval` | justified request that would exceed the remaining per-run budget — a human can raise it |
| `error` | ledger-only status: the call failed (unknown pack/evidence id, no active `kb_version`, or an unexpected exception). Unresolvable `run_id`/`kb_version` values are recorded as the sentinel `"-"` |

Two guarantees worth knowing when you read error rows:

- **A crashed call still lands in the ledger** — the uniform tool wrapper
  (`mcp/tool_handlers.py`) ledgers any exception a handler didn't ledger itself, exactly once, and
  the exception still reaches the caller.
- **A crashed call doesn't eat budget** — any charge made before the failure is refunded under the
  same lock the charge used (`kb_search` restores its call/token counters and logs
  `status=refunded`; the pack-scoped tools restore the pack's run/agent meters). A failing
  platform never silently drains an agent's allowance.

Per-tool detail lives in the `details` JSONB column (migration `0017`): `kb_search` writes
`{session, calls_used, tokens_used, max_requests, max_tokens}` — the budget window state after the
call — and `get_task_context` writes its own per-node breakdown. Useful starting queries:

```sql
-- the last 20 calls, newest first
SELECT tool_name, agent_name, status, tokens_returned, created_at
FROM retrieval_event ORDER BY created_at DESC LIMIT 20;

-- how close a session is to its kb_search budget
SELECT created_at, details
FROM retrieval_event
WHERE tool_name = 'kb_search'
ORDER BY created_at DESC LIMIT 5;

-- error rows and what could not be resolved
SELECT tool_name, run_id, kb_version, created_at
FROM retrieval_event WHERE status = 'error' ORDER BY created_at DESC;
```

## What a gate-blocked build looks like

A `kb_version` activates only after the publish gates pass. When one fails, nothing breaks — the
previous active version keeps serving and the failure is a first-class, queryable outcome:

- **In the build log**: `event=publish_gate_failed gate=<name> ...`, and the CLI tail prints
  `build status : validation_failed` instead of `active`.
- **In Postgres**: the run row records exactly which gate and its measured value:

```sql
SELECT kb_version, status, failed_gate, gate_measured_value, error_summary
FROM kb_build_run
WHERE status IN ('failed', 'validation_failed')
ORDER BY build_seq DESC;
```

- **In the dashboard**: the same columns surface in `v_build_health` (plus
  `extractor_failures` — under ADR-0029's per-source commits a single bad source no longer aborts
  a build; it increments this counter, and the `extractor_error_rate` gate fails the *version*
  only above a 1% threshold).

One build-won't-start case that is not a gate: `build aborted: another builder is running` with
`event=builder_lock_held` in the log — the single-builder Postgres advisory lock. See
[08 — Troubleshooting](08-troubleshooting.md) §"Builder lock held".

## The health check: `make eval-all`

When you want "is the platform actually working?" rather than "what did it do?", run the
consolidated evaluation:

```sh
make eval-all
```

It runs every tier that *can* run in your current shell — deterministic golden sets against a
migrated test registry (T1), zero-LLM checks against a really built KB (T2), the LLM-armed A/B
smoke (T3), and the adversarial-fixture inventory (T4) — and **skips anything unconfigured with a
stated reason** instead of failing or inventing a number. Add T0 (the full `make verify`
lint+types+tests gate) with `cd evals && uv run python run_all.py --with-gates`. The report lands
at `evals/report_all.md`. What each tier proves and how to add cases:
[`docs/architecture/evaluation-system.md`](../architecture/evaluation-system.md).
