# ADR-0014 — Observability + cost-analysis dashboard over the ledger and eval metrics

## Status

**Accepted** (2026-07-05). Ratified by the owner's explicit directive to make observability ready
("I want to make sure observability is ready", 2026-07-05), following the owner's earlier
observability decision (ledger complete by construction, error rows + budget refunds — commit
346c2d2). Open question answered per that context: **operator-only, Phase 1** — aggregate views,
no per-team drill-down by default; per-team self-service remains a Phase 2 decision. One metric
added to the catalog at ratification: **zero/thin-result `kb_search` rate** from the ledger, as the
honest server-side proxy for the ADR-0025 "KB gap" signal (true host-side file-fallback events are
invisible to the server by architecture — hosts fall back natively; this proxy is what the ledger
can actually prove). Phases 2+ stay deferred pending demand.

## Context

We run a Postgres-first, nightly-built knowledge platform fronted by an MCP Context Broker, and we
already record almost everything an operator would want to watch — but only as rows. There is no
view that answers "is retrieval healthy, are we leaking budget, did last night's build publish?"
without hand-written SQL. Three families of signal already exist in Postgres and the eval harness:

- **Retrieval quality** — every broker call writes a `retrieval_event` (status, cache_hit,
  semantic_reuse, returned_artifact_ids plus reused/new evidence id arrays, tokens_returned). The
  golden-query harness
  scores `evidence_recall` and `acl_leak_count` per case (`evals/harness/golden.py`).
- **Token / cost economics** — `context_tokens_per_successful_task`, `duplicate_context_tokens`,
  `evidence_reuse_rate`, `semantic_cache_hit_rate`, `retrieval_calls_per_agent`
  (`evals/harness/metrics.py`), plus per-build `llm_calls` / `embedding_calls` on `kb_build_run`.
- **Build health** — `kb_build_run` records `status`, `sources_seen/changed`, `artifacts_*`,
  `failed_gate` + `gate_measured_value`, `extractor_failures`, and the single active build (partial
  unique index `uq_kb_build_run_single_active` on `status='active'`). `active_kb_age` is the age of
  that active build.

The data is the source of truth (invariant 1). What is missing is a **derived, rebuildable view** of
it. The risk a dashboard exists to catch is exactly the failure mode ADR-0010/0011 flagged:
underlinking and budget drift look fine on the happy path and only show up in aggregate trends
(`evidence_recall` slipping toward the 0.95 publish floor, `duplicate_context_tokens` creeping above
zero, `semantic_cache_hit_rate` falling). Today nobody sees those trends.

The constraint is CLAUDE.md's **V1 exclusion list**: no Azure Functions, Event Grid/Service Bus,
Redis, API Management, Blob Storage, dedicated graph DB, or new always-on service may be introduced
without weighing it here. A dashboard must therefore be a *projection*, never a new store.

## Decision (proposed)

**Phase 1: a read-only metrics module in `evals/` that renders a static report from SQL over the
existing ledger and `kb_build_run`, plus reusable SQL views.** No new service, no new infra, no
excluded resource. Concretely:

1. **Reversible SQL views** (one Alembic migration in `services/kb-builder/migrations/`, since that
   service owns the schema) that aggregate the raw tables into operator-facing rollups:
   `v_retrieval_health`, `v_token_economics`, `v_build_health`, `v_budget_adherence`. Views are pure
   projections — dropping them loses nothing (invariant 1). Per-run/per-agent budget adherence is
   computed by joining `retrieval_event` against the budgets in `.claude/rules/token-budgets.md`
   (e.g. implementation agent 2 requests / 3k–4k tokens), surfacing over-budget runs.
2. **A read-only renderer** (`evals/harness/dashboard.py` + a `--dashboard` flag on `evals/run.py`)
   that runs those views/queries and emits a **static HTML + Markdown report** — the same posture as
   the existing eval report (`docs/contracts/evals-report.md`). It reuses the pinned metric names
   from `evals/harness/metrics.py` and the golden metrics from `evals/harness/golden.py` so build,
   eval, and dashboard never diverge on definitions.
3. **The report is generated on demand and in CI/nightly** via the existing GitHub Actions runner —
   no daemon, no always-on web process. Output is an artifact, regenerable from Postgres at any time.

**Recommendation: Phase 1 only for now.** It is the cheapest option, introduces zero excluded
resources, and ships value (trend visibility, budget-breach detection) without committing to an
interactive surface that may never be needed. Phases 2–3 below are recorded so we don't re-litigate.

### Options weighed

| Option | What it is | Verdict |
| --- | --- | --- |
| **(a) Static report from SQL** | In-repo module renders HTML/MD from views over the ledger | **Chosen (Phase 1).** No new infra; rebuildable; reuses eval metric defs. |
| **(b) Read-only query API on the MCP/web transport** | A few read-only `dashboard.*` tools over the same views, served by the existing fastmcp transport | **Phase 2, if demanded.** No new service (reuses the transport), but adds an interactive surface, an auth/ACL story, and budget for the calls themselves. Needs a contract + tool schema first (mcp-tools rule). |
| **(c) External BI / Grafana on a read replica** | Point Grafana/Metabase at a Postgres read replica | **Phase 3 / rejected for V1.** A read replica + an always-on BI service is net-new infra adjacent to the exclusion list; needs its own ACL story and operational cost. Defer until (a)+(b) prove insufficient. |

### Metrics catalog (metric → source → definition)

Drawn from the pinned names so the dashboard and the eval gates agree.

| Metric | Source table / column(s) | Definition |
| --- | --- | --- |
| `evidence_recall` | golden harness over `retrieval_event.returned_artifact_ids` / `reused_evidence_ids` / `new_evidence_ids` | `|returned ∩ expected| / |expected|`; publish floor 0.95 |
| `acl_leak_count` | golden harness `must_not_leak_ids` vs returned | must be 0 (hard gate) |
| `evidence_reuse_rate` | `retrieval_event.status` (`reused` / `approved`) | reused ÷ reuse-eligible |
| `semantic_cache_hit_rate` | `retrieval_event.semantic_reuse`, `tool_name='context.request_more'` | semantic-reuse follow-ups ÷ charged follow-ups |
| `context_tokens_per_successful_task` | `retrieval_event.tokens_returned` per `run_id` | Σ tokens charged on successful runs ÷ #successful runs |
| `duplicate_context_tokens` | `retrieval_event` evidence-id arrays per run | tokens charged for evidence already delivered in the run; target 0 |
| `retrieval_calls_per_agent` | `retrieval_event.agent_name` | events ÷ distinct agents |
| budget adherence (per run / per agent) | `retrieval_event` × token-budgets rule | runs/agents exceeding the per-run (12k–18k) or per-agent extra limits |
| `llm_calls_per_build` | `kb_build_run.llm_calls` | LLM calls per build run |
| `embedding_calls_per_build` | `kb_build_run.embedding_calls` | embedding calls per build run |
| cache-hit rate (build) | `retrieval_event.cache_hit`; build via llm/embedding calls ÷ `sources_changed` | share of work served from cache |
| publish-gate pass/fail | `kb_build_run.status`, `failed_gate`, `gate_measured_value` | which gate blocked activation and its measured value |
| `extractor_failures` | `kb_build_run.extractor_failures` | files that failed AST extraction in the build |
| `active_kb_age` | active `kb_build_run` (`status='active'`) `completed_at` | wall-clock age of the served KB version |

### Privacy / ACL

The dashboard is **aggregate-only**. It reads ledger *metadata* (statuses, counts, token totals,
evidence **id arrays** and edge **types**) and `kb_build_run` counters — never `query_text`,
`knowledge_artifact.body_text`, or any raw evidence content. It therefore cannot leak artifact text
and does not go through the broker's per-call ACL path. To avoid re-identifying a single team's
activity, team-scoped breakdowns (via `source_item.acl_teams`) are gated: shown only to operators,
suppressed below a small-N threshold, and the default report is corpus-wide aggregates. The dashboard
**must not** be a back door around the broker's ACL model (invariant 6) — it never resolves an id to
its content and never serves to product agents.

## Consequences

- One reversible Alembic migration adds **views only** (no tables/columns); downgrade drops them and
  loses nothing — the underlying rows are untouched (invariants 1, 5).
- Metric definitions live in exactly one place (`evals/harness`) and are imported by the dashboard,
  so a gate and its dashboard tile can never disagree.
- Operators gain trend visibility and budget-breach alerts with no new running service and no
  excluded-V1 resource — the report is a CI/nightly artifact, regenerable from Postgres.
- Cost is bounded: read-only SQL on existing tables; no model calls, no embeddings, no broker budget
  consumed (Phase 1 does not touch the broker path).
- Future Phase 2 (read-only MCP tools) would add per-call budget and an auth/ACL contract — recorded
  here so it is a deliberate, contracted step, not a drift.

## Alternatives rejected

- **A new always-on dashboard service / Azure Function / web app:** introduces an excluded-V1
  resource for a read-only view; rejected in favour of a static artifact on the existing runner.
- **Writing metrics to a new metrics table / time-series store (or Redis):** would make the
  dashboard a second source of truth and add an excluded store; rejected — everything is derivable
  from `retrieval_event` + `kb_build_run`, so the dashboard stays a pure projection.
- **Logging raw `query_text` / evidence body into the report for "drill-down":** breaks the
  aggregate-only ACL posture and risks content leakage; rejected — drill-down stays id/type-level.

## Phasing

- **Phase 1 (on acceptance):** reversible SQL views + the read-only renderer in `evals/` producing a
  static HTML/MD report from existing tables, wired into nightly CI. Reuses pinned metric names.
- **Phase 2 (if interactivity is demanded):** a small set of read-only `dashboard.*` MCP tools over
  the same views on the existing transport — contract + tool schema first (mcp-tools rule), with its
  own auth/ACL and budget for the calls.
- **Phase 3 (only if 1+2 prove insufficient):** evaluate external BI on a read replica via a new
  ADR — this is the point at which net-new infra is on the table and must be justified against the
  exclusion list.

## Open question for the ratifier

Confirm the **owner audience and scope**: is the dashboard operator-only (assumed here, which keeps
the ACL posture simple — aggregate, no team drill-down by default), or must it also serve per-team
self-service views? Per-team self-service raises the ACL story to Phase-2-level (authenticated,
team-scoped, small-N suppressed) and would pull the read-only MCP tools (option b) forward.
