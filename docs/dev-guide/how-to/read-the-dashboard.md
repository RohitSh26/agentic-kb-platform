# Read the dashboard

**Goal:** one command that answers "what happened, and what did it cost?"

## Steps

1. **Render it** — point `DATABASE_URL` at a real, built registry:

   ```sh
   DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" make dashboard
   ```

2. **Open it** — the renderer writes `evals/dashboard.html` (self-contained, no external assets)
   and `evals/dashboard.md`.

## Verify

The command ends with:

```
dashboard written: .../evals/dashboard.html .../evals/dashboard.md
```

and the Markdown opens with an "At a glance" block like this (real output):

```
- [OK] Retrieval events (7d): **209**
- [WARN] Error rate (7d): **1.4%**
- [OK] Evidence reuse rate (7d): **0.0%**
- [OK] KB-gap proxy: kb_search zero/thin (7d): **0.0%**
- [OK] Tokens charged (7d): **150,149**
- [OK] Ledger-mined vs unresolved (7d builds): **0 / 0 (n/a mined)**
- [OK] Budget breaches (runs over / agents over): **0 / 0**
- [OK] Latest build (local.20260707T203326Z): **active**
- [OK] Active KB age: **0.8h**
- [OK] Golden gate (floor 0.95, latest eval run): **mean recall 100.0%, acl_leaks 0**
```

## What you are looking at

The renderer is read-only: it only issues `SELECT`s against the four `v_*` views in the registry
— plus, when `evals/report.json` exists, the latest golden-eval block. It is aggregate-only by
design: statuses, counts, token totals, and evidence id arrays — never query text, never artifact
bodies, never raw evidence content.

| Tile (view) | The question it answers |
|---|---|
| **Retrieval health** (`v_retrieval_health`) | Is retrieval healthy, day by day? Statuses per day, `error_rate`, `evidence_reuse_rate`, and the **KB-gap proxy**: `kb_search_zero_thin_rate` — answered `kb_search` calls that returned ≤ 1 artifact, the ledger's best visible signal that agents are asking for knowledge the KB doesn't have. Also the **mined-vs-unresolved split**: `ledger_mined` / `ledger_unresolved` / `ledger_mined_rate`, rolled up from each build's ledger-mining counters — how many of those misses the build turned into aliases. |
| **Token economics** (`v_token_economics`) | What is context costing? `tokens_charged` per day, `context_tokens_per_run`, `retrieval_calls_per_agent`. |
| **Build health** (`v_build_health`) | Did the builds behave? One row per build: status, duration, `llm_calls_per_changed_source` and `embedding_calls_per_changed_source` (the cache-efficiency signal — near zero on an incremental rebuild), `extractor_failures`, `failed_gate`, and `active_kb_age_seconds` on the single active row. |
| **Budget adherence** (`v_budget_adherence`) | Is any agent over its budget? Per (`run_id`, `agent_name`): tokens charged, follow-up requests/tokens, and `over_run_budget` / `over_agent_requests` / `over_agent_tokens` flags. |

## When the rendered view is too coarse

Query the views directly:

```sh
psql agentic_kb -c "SELECT * FROM v_retrieval_health ORDER BY day DESC LIMIT 7;"
psql agentic_kb -c "SELECT * FROM v_build_health ORDER BY build_seq DESC LIMIT 5;"
psql agentic_kb -c "SELECT * FROM v_budget_adherence WHERE over_run_budget OR over_agent_tokens;"
```

Column-by-column definitions:
[the dashboard contract](../../contracts/observability-dashboard.md). Row-level ledger and trace
queries: [query traces and the ledger](query-traces-and-the-ledger.md). The model behind all of
it: [observability](../explanation/observability.md).
