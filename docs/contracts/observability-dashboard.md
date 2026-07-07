# Contract: Observability dashboard views + renderer (ADR-0014 Phase 1)

Version: 1.0.0. Producer of the views: `services/kb-builder` (migration `0020_dashboard_views`).
Consumer: the read-only renderer `evals/harness/dashboard.py` (`run.py --dashboard`, `make
dashboard`). Operator-only, **aggregate-only**: the views and the renderer read ledger *metadata*
(statuses, counts, token totals, evidence **id arrays**) and `kb_build_run` counters — never
`retrieval_event.query_text` / `normalized_query`, never `knowledge_artifact.body_text`, never any
raw evidence content. The dashboard never resolves an id to content and never serves product agents.

Views are pure projections over `retrieval_event` and `kb_build_run` — dropping them loses nothing
(invariant 1). Renaming a view or a column below is a breaking change to this contract.

Per-step traces (ADR-0032) are a separate, complementary observability surface — the `trace_span`
table, its `TraceSink` port, and how to query it are documented in `tracing.md`; this contract's
views do not read `trace_span`.

## Pinned metric names

Columns that share a name with `evals/harness/metrics.py` / `golden.py` share the **definition**
(ADR-0014's hard rule: a gate and its dashboard tile can never disagree):
`evidence_reuse_rate`, `semantic_cache_hit_rate`, `retrieval_calls_per_agent`, and the golden-gate
floor `DEFAULT_MIN_EVIDENCE_RECALL` (0.95). Ledger-only variants that CANNOT honor a pinned
definition use a distinct name (`context_tokens_per_run`, not `context_tokens_per_successful_task`
— the ledger does not know task success) rather than faking the pinned one.

## `v_retrieval_health` — one row per day over `retrieval_event`

| Column | Definition |
|---|---|
| `day` | `created_at` truncated to date (UTC as stored) |
| `events` | ledger rows that day |
| `approved` / `reused` / `denied` / `needs_human_approval` / `errors` | rows per broker status |
| `error_rate` | `errors / events` |
| `evidence_reuse_rate` | `reused / (reused + approved)`; NULL when no reuse-eligible rows |
| `semantic_cache_hit_rate` | `semantic_reuse` rows among **charged** (`approved`/`reused`) `context.request_more` rows ÷ those rows; NULL when none |
| `cache_hit_rate` | `cache_hit` rows ÷ events |
| `kb_search_answered` | `kb_search` rows with status `approved` |
| `kb_search_zero_thin` | answered `kb_search` rows with ≤ 1 returned artifact id |
| `kb_search_zero_thin_rate` | `kb_search_zero_thin / kb_search_answered`; NULL when none — the **KB-gap proxy** ratified in ADR-0014 (host-side file fallbacks are invisible to the server; an answered-but-empty/thin `kb_search` is what the ledger can prove). Budget `denied` rows are not gaps and are excluded. |
| `ledger_mined` / `ledger_unresolved` | Migration `0023` (ADR-0034, PR-43): that day's `kb_build_run` rows' summed `ledger_mining_mined` / `ledger_mining_unresolved` counters (0 on a day with no build). **Not** a per-event join against `kb_search_zero_thin` — see below. |
| `ledger_mined_rate` | `ledger_mined / (ledger_mined + ledger_unresolved)`; NULL when neither is > 0 |

### Mined-vs-unresolved split — why it is a `kb_build_run` roll-up, not a per-event flag

The ledger-mining build step (`alias/ledger_mining.py`, `docs/contracts/alias-reference.md` "Ledger-
mined aliases") reads `retrieval_event` misses and classifies each DISTINCT phrase as `mined`
(resolved to a target — a new/refreshed alias, or already covered by an existing one) or
`unresolved` (no match). Two hard constraints shape how that split reaches the dashboard:

1. kb-builder **never writes `retrieval_event`** — that table's runtime-write ownership is
   mcp-server's alone (`postgres-knowledge-registry.md`); a build-time actor inserting rows there
   would violate the boundary this contract and that one both pin.
2. The aggregate-only ACL posture (above) forbids any view SQL from touching
   `retrieval_event.query_text` / `normalized_query` — which is the only column that could join an
   individual historical miss row to the alias phrase that later resolved it. There is therefore no
   privacy-safe way to retroactively flag EXISTING `kb_search` rows as "now mined".

Given both, the split is instead persisted as three counters kb-builder ALREADY has write access to
— `kb_build_run.ledger_mining_misses_seen` / `ledger_mining_mined` / `ledger_mining_unresolved`
(migration `0022`, plain `INTEGER NOT NULL DEFAULT 0` columns, same idiom as the existing
`extractor_failures` / `llm_calls` build counters) — populated once per build from the SAME
`LedgerMiningResult` the structured completion log reports. `v_retrieval_health` (migration `0023`)
LEFT JOINs a `date_trunc('day', kb_build_run.started_at)` roll-up of those three counters onto its
existing per-day `retrieval_event` aggregate, on `day`. This reads only `retrieval_event` and
`kb_build_run` (the two tables this whole contract is pinned to), never `knowledge_artifact` /
`body_text` / `query_text` / `normalized_query`. The trade-off: the split answers "how much did
LAST NIGHT'S build resolve" (build-grain, correct and exact), not "which of TODAY's individual miss
events are now fixed" (per-event, would require the privacy-unsafe join) — the existing
`kb_search_zero_thin` / `kb_search_zero_thin_rate` columns remain the one honest per-event gap
signal; `ledger_mined` / `ledger_unresolved` are the build's own account of what it did about it.

## `v_token_economics` — one row per day over `retrieval_event`

| Column | Definition |
|---|---|
| `day` | as above |
| `runs` | distinct `run_id`, **excluding** the broker's no-run sentinel `'-'` (kb_search / session-scoped tools) — the sentinel is not a run and would skew per-run tokens |
| `agents` / `events` | distinct `agent_name` / row count (corpus-wide, sentinel included) |
| `tokens_charged` | Σ `coalesce(tokens_returned, 0)` over all rows — matches the harness `RunRecord.tokens_charged` (denied/error rows carry 0; kb_search refunds never reach the ledger) |
| `context_tokens_per_run` | Σ tokens on **non-sentinel** rows ÷ `runs`; NULL when no real runs (ledger-only variant; see pinned-names note) |
| `retrieval_calls_per_agent` | `events / agents` (pinned definition) |

## `v_build_health` — one row per `kb_build_run`

`build_id`, `kb_version`, `build_seq`, `status`, `started_at`, `completed_at`,
`duration_seconds`, `sources_seen`, `sources_changed`, `artifacts_created/updated/deleted`,
`llm_calls`, `embedding_calls`, `llm_calls_per_changed_source`,
`embedding_calls_per_changed_source` (both NULL when `sources_changed = 0` — the incremental-build
cache-efficiency signal), `extractor_failures`, `failed_gate`, `gate_measured_value`,
`error_summary`, `is_active`, and `active_kb_age_seconds` (`now() - completed_at` on the single
`status='active'` row, NULL elsewhere — the pinned `active_kb_age`).

Out of scope for this view (PR-43): `kb_build_run.ledger_mining_misses_seen` /
`ledger_mining_mined` / `ledger_mining_unresolved` (migration `0022`) are NOT projected here — only
their day roll-up in `v_retrieval_health` (below). Per-build values remain queryable directly on
`kb_build_run` or from that build's `event=ledger_mining_completed` structured log line.

## `v_budget_adherence` — one row per (`run_id`, `agent_name`)

Joins ledger aggregates against the budget numbers in `.claude/rules/token-budgets.md`. A view
cannot read a rules file, so the current numbers are encoded as **literals** in the view;
kb-builder's `tests/integration/test_dashboard_views.py` parses the rules file and fails on drift
(the ALLOWED_EDGE_TYPES precedent). Rows with the broker's no-run sentinel `run_id = '-'`
(`kb_search` and unresolved-error rows) are **excluded**: the sentinel is not a run, and kb_search's
budget is enforced per session window server-side (its denials remain visible in
`v_retrieval_health.denied`).

| Column | Definition |
|---|---|
| `run_id`, `agent_name` | grain |
| `events` | ledger rows for the pair |
| `tokens_charged` | Σ `coalesce(tokens_returned, 0)` for the pair |
| `run_tokens` | Σ `tokens_charged` over the whole run |
| `run_budget_tokens` | literal 18000 — upper bound of the 12k–18k full-run band |
| `over_run_budget` | `run_tokens > run_budget_tokens` |
| `follow_up_requests` / `follow_up_tokens` | **charged** (`approved`/`reused`) `context.request_more` rows / their token sum |
| `agent_max_requests` / `agent_max_tokens` | the agent's extra allowance (upper bounds): implementation 2/4000, test_layer 1/2500, code_reviewer 1/2500, delivery_planner 1/1500, pr_planner 1/1500; unmapped agents get the broker default 1/2500 (`budgets.DEFAULT_AGENT_ALLOWANCE`) |
| `over_agent_requests` / `over_agent_tokens` | follow-up usage above the allowance |

## Renderer (`evals/harness/dashboard.py`)

Read-only: issues SELECTs against the four views (explicit column lists, never `SELECT *`, never a
content column — statically asserted in `evals/tests/test_dashboard.py`) plus, when
`evals/report.json` exists, the latest golden block (mean/min evidence recall vs the 0.95 floor,
ACL leaks). Emits `dashboard.html` (self-contained, no external assets) and `dashboard.md` to an
output directory (default `evals/`, override `--dashboard-out`). Never writes to any table.
Connection: `DATABASE_URL` if set (safe — SELECT-only), else `TEST_DATABASE_URL`. Every render
logs a structured `dashboard.generated` line.
