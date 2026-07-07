# Database reference

The Knowledge Registry is a plain Postgres database — the platform's single source of truth.
Everything lives in the `public` schema, owned by kb-builder's Alembic migrations (head: `0023`).
Ground truth for shapes:
[`postgres-knowledge-registry.md`](../../contracts/postgres-knowledge-registry.md) and
`services/kb-builder/migrations/versions/`.

## Tables

```
psql agentic_kb -c '\dt'
```

| Table | One line |
|---|---|
| `source_item` | What was fetched: one row per file/doc/wiki page/card/commit. `content_hash` drives the incremental skip; `repo`/`branch`/`external_id` carry provenance; `is_deleted` tombstones. |
| `knowledge_artifact` | Every node in the knowledge graph, one table for every kind, distinguished by `artifact_type`. |
| `knowledge_edge` | Every edge between two artifacts: `edge_type`, `source` (producer), `trust_class`, `confidence`, `evidence`, `relation_schema_version`, and the validity interval. |
| `kb_build_run` | One row per build: status, counters, publish-gate outcome, and the monotonic `build_seq` that defines version membership. |
| `retrieval_event` | The retrieval ledger: one row per broker tool call, written by mcp-server only. The audit record — treat as never pruned without an explicit retention decision. |
| `trace_span` | Per-step tracing (ADR-0032): one row per traced unit of work inside `get_task_context`/`kb_search`. Pure derived observability — safe to prune by age. |
| `generation_cache` / `generation_cache_artifact` | Gates every docify LLM call; a hit reuses the previously produced artifacts. |
| `embedding_cache` | Gates every embedding call; stores the vector so the index rebuilds without re-embedding. |
| `doc_extraction_output` / `embedding_output` | The crash-durable model-output cache (fail-soft; a cache problem degrades to a paid call, never a crashed build). |
| `relationship_candidate` | Phase-3A audit: bounded cross-domain candidate pairs. Candidates are measurements, not edges. |
| `relationship_judgment_cache` | Gates the phase-3B LLM judge; an unchanged pair is never re-judged. |
| `entailment_cache` | Gates the verifier's optional L3 entailment; an unchanged claim makes zero model calls. |
| `alembic_version` | The migration stamp — `0023` when current. |

Caches and audit tables are safe to be empty and never hand-edited.

## Views

Four read-only aggregates over `retrieval_event` / `kb_build_run` — pure projections; dropping
them loses nothing:

| View | The question it answers |
|---|---|
| `v_retrieval_health` | Is retrieval healthy, day by day? Statuses, `error_rate`, `evidence_reuse_rate`, the KB-gap proxy `kb_search_zero_thin_rate`, and the ledger-mining split (`ledger_mined` / `ledger_unresolved` / `ledger_mined_rate`). |
| `v_token_economics` | What is context costing? `tokens_charged` per day, `context_tokens_per_run`, `retrieval_calls_per_agent`. |
| `v_build_health` | Did the builds behave? Per-build status, duration, cache-efficiency ratios, `failed_gate`, `active_kb_age_seconds`. |
| `v_budget_adherence` | Is any agent over budget? Per (`run_id`, `agent_name`) tokens + over-budget flags. |

## The `review_panel` schema

The review panel owns a **separate schema** (`review_panel`, not `public`) inside whatever
database `REVIEW_PANEL_DATABASE_URL` points at — often the same Postgres instance, but check your
env before assuming the same database. It bootstraps itself on first use (`CREATE SCHEMA IF NOT
EXISTS`), so it won't exist until the panel has run at least once. This is the repo's one
documented Alembic exemption: it holds only derived, recomputable state, and its rollback is
`DROP SCHEMA review_panel CASCADE`. It contains the LangGraph checkpointer tables,
`review_panel.trace_span`, and the draft store:

```sql
SELECT draft_key, repo, pr_number, head_sha, created_at
FROM review_panel.review_draft
ORDER BY created_at DESC LIMIT 20;
```

`draft_key` is `<repo>#<pr_number>@<head_sha>` — at most one row per key (first-writer-wins). The
`draft` JSONB column holds the `review_draft_v1` document
([`review-panel.md`](../../contracts/review-panel.md)).

## Key semantics

**`artifact_type` values.** From the code extractor: `code_file`, `code_symbol`, `endpoint`,
`test`. From doc extraction: `summary`, `concept`, `source_backed_fact`, `chunk`. From git
metadata: `commit`. From the alias miner: `alias_reference`. From the runtime: `evidence_card`.

**`knowledge_kind`.** `interpreted` (LLM-generated; ranks below source-backed evidence) or
`source_backed` (extracted directly from a source at a version).

**Edge fields.** `edge_type` (e.g. `defined_in`, `calls`, `imports`, `uses`, `references`,
`inherits`, `documents`, `implements`, `mentions`, `aliases`, `tests`), `source` (which pass
produced it: `graphify` \| `linker` \| `alias_miner` \| `llm_judge` \| `manual`), `trust_class`
(`EXTRACTED` < `INFERRED_HIGH` < `INFERRED_LOW` < `AMBIGUOUS` < `REJECTED`; deterministic
producers may assign only `EXTRACTED`, the LLM judge never `EXTRACTED`), `confidence` (honest:
fixed high for deterministic matches, the raw score for semantic ones), `evidence` (the textual
justification pointer), `relation_schema_version`.

**Interval membership.** A KB version is a validity interval, not a creation label. Every
artifact/edge carries `valid_from_seq` (the build that introduced it) and `invalidated_at_seq`
(`NULL` while live). A row is a member of the version whose `build_seq = S` iff:

```
valid_from_seq <= S AND (invalidated_at_seq IS NULL OR invalidated_at_seq > S)
```

Rows are stamped once and immutable; prior active versions stay reconstructable. The one
deliberate exception is `acl_teams`, overwritten in place on live rows so a revoked permission
takes effect on every still-served version.

**`kb_build_run` counters.** `sources_seen`, `sources_changed`, `artifacts_created`, `llm_calls`,
`embedding_calls`, `search_docs_upserted`, `extractor_failures`, `failed_gate` +
`gate_measured_value`, `error_summary`, and the ledger-mining counters
`ledger_mining_misses_seen` / `ledger_mining_mined` / `ledger_mining_unresolved`.

**`retrieval_event` columns.** `tool_name`, `agent_name`, `run_id` (`"-"` sentinel for
session-scoped tools like `kb_search`), `kb_version`, `status`, `tokens_returned`,
`returned_artifact_ids`, `details` (JSONB, per-tool), `created_at`. Status values:

| Status | Meaning |
|---|---|
| `approved` | New evidence retrieved and charged against the budget. |
| `reused` | The question matched a previous retrieval — returned at no budget cost. |
| `denied` | A budget said no — a contractual outcome, not an error. |
| `needs_human_approval` | A justified request that would exceed the remaining per-run budget. |
| `error` | The call failed; unresolvable `run_id`/`kb_version` recorded as `"-"`. Crashed calls are ledgered exactly once and their budget charge refunded. |

`details` shapes: `kb_search` writes `{session, calls_used, tokens_used, max_requests,
max_tokens}` — the budget window after the call; `get_task_context` writes its per-node latency
breakdown plus result counts.

**`trace_span` columns.** `span_id`, `trace_id`, nullable `parent_span_id` (NULL = root), `name`,
`service`, `started_at`/`ended_at`, `status` (`ok`|`error`), aggregate-only `attributes` (the
constructor rejects content-shaped keys).

## Which database is which

| Database | Role |
|---|---|
| `agentic_kb` | The bootstrap default — the one you build into, serve, and browse. |
| `agentic_kb_test` | The shared database `make verify`/`make test-*` migrate **up and down** against. Never browse or keep data in it — kb-builder's suite downgrades it to base on teardown. `make test-mcp-server`/`make test-evals` re-migrate it first (`make migrate-test-db`); do the same before a manual psql session. |
| anything else | Scratch — disposable experiments, safe to drop when unrecognized. |

```sh
psql -lqt | cut -d '|' -f1   # every database on this instance
```

## Query cookbook

Copy-paste queries, organized by question. All run against your build database (`psql agentic_kb`).

### Health at a glance

```sql
-- the active version and its age (exactly one row, guaranteed by a partial unique index)
SELECT kb_version, status, started_at, completed_at, now() - completed_at AS active_age
FROM kb_build_run WHERE status = 'active';

-- recent builds and their cost/outcome
SELECT kb_version, build_seq, status, sources_seen, sources_changed, artifacts_created,
       llm_calls, embedding_calls, extractor_failures, failed_gate, gate_measured_value
FROM kb_build_run ORDER BY build_seq DESC LIMIT 10;

-- failed publishes are first-class, queryable outcomes
SELECT kb_version, status, failed_gate, gate_measured_value, error_summary
FROM kb_build_run WHERE status IN ('failed', 'validation_failed') ORDER BY build_seq DESC;
```

Healthy: exactly one `active` row, `failed_gate IS NULL`, `artifacts_created > 0`. A gate-blocked
build shows `status = validation_failed` with the gate named; a crashed build shows
`status = failed` with the exception in `error_summary`. Either way the previous active version
keeps serving.

### Artifacts

```sql
SELECT artifact_type, count(*) FROM knowledge_artifact GROUP BY 1 ORDER BY 2 DESC;

-- artifacts per source type
SELECT s.source_type, a.artifact_type, count(*)
FROM knowledge_artifact a JOIN source_item s ON s.source_id = a.source_id
GROUP BY 1, 2 ORDER BY 1, 3 DESC;
```

A default (code-only) build of this repository shows:

```
  artifact_type  | count
-----------------+-------
 code_symbol     |  4663
 alias_reference |  3278
 code_file       |   410
 commit          |   200
```

### Edges

```sql
SELECT edge_type, source, trust_class, count(*)
FROM knowledge_edge GROUP BY 1, 2, 3 ORDER BY 1;

-- every linker edge should carry an evidence pointer
SELECT count(*) AS edges_missing_evidence
FROM knowledge_edge WHERE source = 'linker' AND evidence IS NULL;

-- inspect cross-references with titles
SELECT e.edge_type, e.trust_class, a.title AS from_title, b.title AS to_title, e.evidence
FROM knowledge_edge e
JOIN knowledge_artifact a ON a.artifact_id = e.from_artifact_id
JOIN knowledge_artifact b ON b.artifact_id = e.to_artifact_id
ORDER BY e.edge_type LIMIT 40;
```

### Cross-domain and inferred links

```sql
-- commit → work-item / code_file links from git metadata
SELECT e.edge_type, c.title AS commit_title, t.title AS target, e.evidence
FROM knowledge_edge e
JOIN knowledge_artifact c ON c.artifact_id = e.from_artifact_id AND c.artifact_type = 'commit'
JOIN knowledge_artifact t ON t.artifact_id = e.to_artifact_id
LIMIT 30;

-- phase-3A candidates (measurement only — NOT edges)
SELECT count(*) FROM relationship_candidate;

-- phase-3B inferred edges from the LLM judge (routing hints, never claim support)
SELECT trust_class, count(*) FROM knowledge_edge WHERE source = 'llm_judge' GROUP BY 1;

-- judge cache: a hit means zero LLM calls on re-judge
SELECT count(*) FROM relationship_judgment_cache;
```

### Version membership — the served set

The set the broker serves must be **complete** after an incremental build, not just the delta:

```sql
WITH active AS (SELECT build_seq FROM kb_build_run WHERE status = 'active')
SELECT
  count(*) FILTER (
    WHERE a.valid_from_seq <= active.build_seq
      AND (a.invalidated_at_seq IS NULL OR a.invalidated_at_seq > active.build_seq)
  ) AS served_by_active_version,
  count(*) AS total_artifacts_all_versions
FROM knowledge_artifact a, active;

-- what the active build invalidated (deletes/renames/supersession)
WITH active AS (SELECT build_seq FROM kb_build_run WHERE status = 'active')
SELECT artifact_type, count(*) FROM knowledge_artifact, active
WHERE invalidated_at_seq = active.build_seq GROUP BY 1;
```

### Ghost edges (expect 0)

An edge whose endpoint isn't a live member of the active version:

```sql
WITH active AS (SELECT build_seq FROM kb_build_run WHERE status = 'active'),
members AS (
  SELECT artifact_id FROM knowledge_artifact, active
  WHERE valid_from_seq <= build_seq
    AND (invalidated_at_seq IS NULL OR invalidated_at_seq > build_seq)
)
SELECT count(*) AS ghost_edges
FROM knowledge_edge e, active
WHERE e.valid_from_seq <= active.build_seq
  AND (e.invalidated_at_seq IS NULL OR e.invalidated_at_seq > active.build_seq)
  AND ( e.from_artifact_id NOT IN (SELECT artifact_id FROM members)
     OR e.to_artifact_id   NOT IN (SELECT artifact_id FROM members) );
```

Non-zero means a publish gate should have blocked activation — check the build-run rows.

### Cache and cost per build

```sql
-- an incremental rebuild should be ~0 llm_calls
SELECT build_seq, kb_version, llm_calls, embedding_calls, sources_changed
FROM kb_build_run ORDER BY build_seq DESC LIMIT 10;

SELECT count(*) AS generation_cache_rows FROM generation_cache;
SELECT count(*) AS embedding_cache_rows  FROM embedding_cache;
```

### Sources, freshness, ACLs, provenance

```sql
SELECT source_type, count(*) FILTER (WHERE NOT is_deleted) AS live,
       count(*) FILTER (WHERE is_deleted) AS deleted
FROM source_item GROUP BY 1;

-- restricted artifacts and their teams
SELECT artifact_type, acl_teams, count(*)
FROM knowledge_artifact WHERE acl_teams <> '{}' GROUP BY 1, 2;

-- per-source-type freshness
SELECT source_type, count(*) AS sources, max(last_seen_at) AS most_recent
FROM source_item WHERE NOT is_deleted GROUP BY 1 ORDER BY 1;

-- provenance: every code/doc source should carry a repo (expect zero rows)
SELECT source_type, count(*) FROM source_item WHERE repo IS NULL GROUP BY 1;
```

### The alias index

```sql
SELECT count(*) FROM knowledge_artifact WHERE artifact_type = 'alias_reference';

SELECT title, body_text::json ->> 'confirmation_count' AS confirmations
FROM knowledge_artifact WHERE artifact_type = 'alias_reference'
ORDER BY (body_text::json ->> 'confirmation_count')::int DESC NULLS LAST
LIMIT 10;
```

### One-shot rollup

```sql
SELECT
  (SELECT count(*) FROM kb_build_run WHERE status = 'active')       AS active_versions, -- want 1
  (SELECT count(*) FROM kb_build_run WHERE failed_gate IS NOT NULL) AS builds_with_failed_gate,
  (SELECT count(*) FROM knowledge_artifact)                         AS artifacts,
  (SELECT count(*) FROM knowledge_edge)                             AS edges,
  (SELECT count(*) FROM relationship_candidate)                     AS candidates;
```

Ledger and trace queries live in
[query-traces-and-the-ledger](../how-to/query-traces-and-the-ledger.md).

## Migration status

```sh
cd services/kb-builder
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" uv run alembic current
```

Expect `0023 (head)`. Anything older: `uv run alembic upgrade head`.

## Maintenance

```sql
-- size by table
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC LIMIT 15;

-- whole database
SELECT pg_size_pretty(pg_database_size('agentic_kb'));
```

Autovacuum handles routine bloat for this workload; run a manual `VACUUM ANALYZE <table>` only
after a large bulk delete so the planner's statistics catch up immediately.

**Pruning.** `trace_span` is derived observability — safe to prune by age
(`DELETE FROM trace_span WHERE started_at < now() - interval '90 days';` — dry-run with a
`SELECT count(*)` first). `retrieval_event` is the audit ledger; never prune it without an
explicit retention decision. Never run ad hoc `DELETE`s against `kb_build_run` or the
`knowledge_*` tables — supersession is handled by the build engine via the validity interval,
never a hard delete.
