# Tutorial 1 — Explore what got built

[Getting started](../getting-started.md) left you with a built, active knowledge base in the
`agentic_kb` database. This tutorial shows you what is inside it: the tables, the artifacts and
edges extracted from your code, the alias index that powers retrieval, and the one-command
dashboard. Everything here is read-only.

## 1. Connect and list the tables

```sh
psql agentic_kb
```

Inside psql, list the tables:

```
agentic_kb=# \dt
```

**You should see:**

```
                  List of relations
 Schema |            Name             | Type  | Owner
--------+-----------------------------+-------+-------
 public | alembic_version             | table | edhaa
 public | doc_extraction_output       | table | edhaa
 public | embedding_cache             | table | edhaa
 public | embedding_output            | table | edhaa
 public | entailment_cache            | table | edhaa
 public | generation_cache            | table | edhaa
 public | generation_cache_artifact   | table | edhaa
 public | kb_build_run                | table | edhaa
 public | knowledge_artifact          | table | edhaa
 public | knowledge_edge              | table | edhaa
 public | relationship_candidate      | table | edhaa
 public | relationship_judgment_cache | table | edhaa
 public | retrieval_event             | table | edhaa
 public | source_item                 | table | edhaa
 public | trace_span                  | table | edhaa
(15 rows)
```

And the views with `\dv`:

```
             List of relations
 Schema |        Name        | Type | Owner
--------+--------------------+------+-------
 public | v_budget_adherence | view | edhaa
 public | v_build_health     | view | edhaa
 public | v_retrieval_health | view | edhaa
 public | v_token_economics  | view | edhaa
(4 rows)
```

The map, in one paragraph: `source_item` is what was fetched — one row per file, doc, wiki page,
ticket, or commit, with the content hash that drives incremental rebuilds. `knowledge_artifact`
is every node in your knowledge graph. `knowledge_edge` is every edge between two nodes.
`kb_build_run` is one row per build — status, counters, gate outcomes. `retrieval_event` is the
ledger: one row per tool call an agent makes, written by the server. `trace_span` is per-step
timing inside those calls. Everything else is a cache or audit table that gates model calls —
safe to be empty, never hand-edited. The four `v_*` views are read-only aggregates the dashboard
renders. The full column-level reference is [the database reference](../reference/database.md).

## 2. Check the active version

Exactly one build is ever "active" — the one the server serves:

```sql
SELECT kb_version, status, started_at, completed_at,
       now() - completed_at AS active_age
FROM kb_build_run WHERE status = 'active';
```

**You should see** one row:

```
       kb_version       | status |          started_at           |         completed_at          |   active_age
------------------------+--------+-------------------------------+-------------------------------+-----------------
 local.20260707T213652Z | active | 2026-07-07 16:36:52.332042-05 | 2026-07-07 16:37:27.086906-05 | 00:00:41.818349
(1 row)
```

## 3. Count your artifacts and edges

```sql
SELECT artifact_type, count(*) FROM knowledge_artifact
WHERE invalidated_at_seq IS NULL GROUP BY 1 ORDER BY 2 DESC;

SELECT count(*) AS edges FROM knowledge_edge WHERE invalidated_at_seq IS NULL;
```

**You should see** (counts vary with the size of your checkout):

```
  artifact_type  | count
-----------------+-------
 code_symbol     |  4663
 alias_reference |  3278
 code_file       |   410
 commit          |   200
(4 rows)

 edges
-------
 24950
(1 row)
```

Four artifact types from the default build: every function and class (`code_symbol`), every
source file (`code_file`), every commit (`commit`), and the alias index (`alias_reference`,
next step). A build that includes doc summaries adds `summary`, `concept`, and
`source_backed_fact` rows — see [switch LLM providers](../how-to/switch-llm-providers.md) and
`./scripts/bootstrap.sh --with-docs`.

## 4. Meet the alias index

`alias_reference` rows are the deterministic index that resolves the phrases people actually
type — "the review panel", "kb builder" — to the right artifact. Each alias records how many
independent sources confirmed it:

```sql
SELECT title, body_text::json ->> 'confirmation_count' AS confirmations
FROM knowledge_artifact WHERE artifact_type = 'alias_reference'
ORDER BY (body_text::json ->> 'confirmation_count')::int DESC NULLS LAST
LIMIT 5;
```

**You should see** your codebase's own most-confirmed names (this sample is from a build that
includes this repo's docs):

```
            title            | confirmations
-----------------------------+---------------
 mcp tools contract          | 15
 kb builder                  | 15
 dev guide                   | 12
 postgres knowledge registry | 11
 review panel                | 9
(5 rows)
```

This is the same table the server's `kb_search` and `get_task_context` resolution path reads —
the 25/25 check at the end of bootstrap ran against it.

## 5. Read the build's own accounting

```sql
SELECT kb_version, build_seq, status, sources_seen, sources_changed,
       artifacts_created, llm_calls, embedding_calls, failed_gate
FROM kb_build_run ORDER BY build_seq DESC;
```

**You should see:**

```
       kb_version       | build_seq | status | sources_seen | sources_changed | artifacts_created | llm_calls | embedding_calls | failed_gate
------------------------+-----------+--------+--------------+-----------------+-------------------+-----------+-----------------+-------------
 local.20260707T213652Z |         1 | active |          610 |             610 |              5273 |         0 |            2812 |
(1 row)
```

Two things to notice. `llm_calls` is **0**: the default build makes zero model calls — code and
commits are extracted deterministically, and embeddings are a free local hash. `failed_gate` is
empty: the build passed every publish gate before activating (a gate-blocked build never
activates, and the previous version keeps serving — see
[how your knowledge base is built](../explanation/how-your-knowledge-base-is-built.md)).

## 6. Render the dashboard

Quit psql (`\q`), then from the repo root:

```sh
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" make dashboard
```

**You should see** it write `evals/dashboard.html` and `evals/dashboard.md`. The Markdown
version opens with:

```
## At a glance

- [WARN] Retrieval events (7d): **0**
- [OK] Error rate (7d): **n/a**
- [OK] Evidence reuse rate (7d): **n/a**
- [OK] KB-gap proxy: kb_search zero/thin (7d): **n/a**
- [OK] Tokens charged (7d): **0**
- [OK] Ledger-mined vs unresolved (7d builds): **0 / 0 (n/a mined)**
- [OK] Budget breaches (runs over / agents over): **0 / 0**
- [OK] Latest build (local.20260707T213652Z): **active**
- [OK] Active KB age: **0.0h**
- [OK] Golden gate (floor 0.95, latest eval run): **mean recall 100.0%, acl_leaks 0**
```

The `[WARN] Retrieval events (7d): 0` is expected right now — no agent has asked your knowledge
base anything yet. Tile-by-tile meaning: [read the dashboard](../how-to/read-the-dashboard.md).

## Next

Give an agent access and watch that retrieval counter move:
[Tutorial 2 — Ask your first questions](02-ask-your-first-questions.md).
