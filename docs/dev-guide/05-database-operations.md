# 05 — Database operations

Task-first Postgres recipes for running this platform locally: connect, check health, back up,
reset, query, and maintain the Knowledge Registry — exact, copy-paste commands plus what you
should see back, no `psql` fluency assumed. Every command on this page was run against a real
local database while writing it.

This page is **operations** — for the build-time query reference (artifact/edge/cache inspection
while developing a build) see
[22 — Testing and builds §8](22-testing-and-builds.md#8-query-the-database--checks-analysis-did-it-work);
for the retrieval ledger, traces, and the dashboard see
[06 — Observability](06-observability.md). Schema ground truth:
[`docs/contracts/postgres-knowledge-registry.md`](../contracts/postgres-knowledge-registry.md)
and `services/kb-builder/migrations/versions/` (head `0021`).

---

## 1. Connect & orient

Connect with `psql <database-name>` — on macOS/Homebrew Postgres this "just works" via a local
socket, authenticated as your OS user, no password:

```sh
psql agentic_kb
```

If that fails (wrong role, remote/Docker Postgres), be explicit:

```sh
psql -h localhost -p 5432 -U "$USER" -d agentic_kb
# Docker compose's Postgres instead uses port 55432 and role postgres:postgres:
psql -h localhost -p 55432 -U postgres -d agentic_kb
```

Once connected, list the tables:

```
agentic_kb=# \dt
```

What you should see — the Knowledge Registry's tables, all in the `public` schema (a freshly
migrated, unbuilt database shows the same table list with zero rows):

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

And the four dashboard views (`\dv`):

```
             List of relations
 Schema |        Name        | Type | Owner
--------+--------------------+------+-------
 public | v_budget_adherence | view | edhaa
 public | v_build_health     | view | edhaa
 public | v_retrieval_health | view | edhaa
 public | v_token_economics  | view | edhaa
```

**The table map, in one paragraph.** `source_item` is what was fetched (one row per GitHub
file/doc, wiki page, ADO card, or local commit) — `content_hash` drives the incremental skip.
`knowledge_artifact` is every node in the knowledge graph (chunks, summaries, concepts,
source-backed facts, code symbols/files, commits, and `alias_reference` rows) — one table for
every kind, distinguished by `artifact_type`. `knowledge_edge` is every edge between two
artifacts (`edge_type`, `confidence`, `source` = which pass produced it). `kb_build_run` is one
row per nightly/local build — status, counters, publish-gate outcome, and the `build_seq` that
defines which artifacts/edges are "live" in the active version. `retrieval_event` is the ledger:
one row per MCP tool call, written by mcp-server (not kb-builder). `trace_span` is per-step
tracing (ADR-0032) — one row per traced unit of work inside `get_task_context`/`kb_search`. The
rest (`generation_cache`, `embedding_cache`, `doc_extraction_output`, `embedding_output`,
`relationship_candidate`, `relationship_judgment_cache`, `entailment_cache`) are **caches and
audit tables** that gate or record model calls — safe to be empty, never hand-edited. The four
`v_*` views (`v_retrieval_health`, `v_token_economics`, `v_build_health`, `v_budget_adherence`)
are read-only aggregates over `retrieval_event`/`kb_build_run` — dropping and recreating them
(an Alembic downgrade/upgrade) loses nothing. **`review_panel` is a separate schema, possibly in
a separate database** — see §5's "review_panel drafts list" for why and how to reach it.

**Which database is which.** `scripts/bootstrap.sh` and the kb-builder dev-guide default to a
database named **`agentic_kb`** — the one you build into and browse. **`agentic_kb_test`** is the
shared database `make verify`/`make test-*` migrate up and down against (§7 — do not browse it,
its schema gets torn down between test runs). Anything else you or a script created ad hoc
(`agentic_kb_<name>_test`, `agentic_kb_demo`, `agentic_kb_<initiative>`, …) is scratch — safe to
drop any time you don't recognize it or don't need it anymore:

```sh
psql -lqt | cut -d '|' -f1   # every database on this Postgres instance, one per line
```

---

## 2. Health at a glance

Four queries answer "is this KB OK?" — run against your build database (e.g. `agentic_kb`):

**The active version and its age:**

```sql
SELECT kb_version, status, started_at, completed_at,
       now() - completed_at AS active_age
FROM kb_build_run WHERE status = 'active';
```

What you should see — exactly one row (the partial unique index `uq_kb_build_run_single_active`
guarantees this):

```
       kb_version       | status |          started_at           |         completed_at          |       active_age
------------------------+--------+-------------------------------+-------------------------------+------------------------
 local.20260705T141922Z | active | 2026-07-05 09:19:22.802627-05 | 2026-07-05 09:30:13.033746-05 | 2 days 03:49:55.318446
```

**Artifact and edge counts by type:**

```sql
SELECT artifact_type, count(*) FROM knowledge_artifact GROUP BY 1 ORDER BY 2 DESC;
SELECT edge_type, source, count(*) FROM knowledge_edge GROUP BY 1, 2 ORDER BY 1;
```

What you should see — a handful of `artifact_type` rows (`alias_reference`, `code_symbol`,
`code_file`, `commit`, `source_backed_fact`, `summary`, `concept`, …) each with a nonzero count,
and edges grouped by the pass that produced them (`graphify` for code edges, `linker` for
doc↔code, `alias_miner` for the alias index). Zero artifacts means the build never ran or never
activated — go check `kb_build_run` next.

**Source counts by type:**

```sql
SELECT source_type, count(*) FILTER (WHERE NOT is_deleted) AS live,
       count(*) FILTER (WHERE is_deleted) AS deleted
FROM source_item GROUP BY 1;
```

**Last build's gate outcome — including a FAILED example.** A healthy build has `failed_gate
IS NULL`. When a publish gate blocks activation, the row tells you exactly which one and its
measured value (the previous active version keeps serving — invariant 5):

```sql
SELECT kb_version, status, failed_gate, gate_measured_value, error_summary
FROM kb_build_run
WHERE status IN ('failed', 'validation_failed')
ORDER BY build_seq DESC;
```

A gate-blocked build looks like this (captured from a different local database that hit the
edge-evidence-integrity gate during development — this is the shape to expect, not a live
example in a healthy DB):

```
       kb_version       |      status       |       failed_gate       | gate_measured_value | error_summary
------------------------+-------------------+--------------------------+----------------------+---------------
 local.20260703T071258Z | validation_failed | edge_evidence_integrity |                 8898 |
```

A **crashed** build (an exception, not a gate) looks different — `status = 'failed'`,
`failed_gate IS NULL`, and the exception text lands in `error_summary`:

```
       kb_version       | status | failed_gate | gate_measured_value |                       error_summary
------------------------+--------+-------------+----------------------+-------------------------------------------------------------
 local.20260617T051053Z | failed |             |                      | IntegrityError: ... duplicate key value violates unique
                         |        |             |                      | constraint "uq_knowledge_edge_linker" ...
```

Either way, nothing to panic about by itself: the previous `active` row is untouched and still
being served. Full gate list: [`docs/contracts/publish-gates.md`](../contracts/publish-gates.md).

---

## 3. Backup & restore

**Read this first: do you actually need a backup?** Invariant 1 — Postgres is the source of
truth, but the Knowledge Registry itself is **rebuildable from source** (GitHub/wiki/ADO/local
git). If all you'd lose is derived knowledge (artifacts, edges, caches), a rebuild
(`uv run python -m agentic_kb_builder.build ...`, §4 below) is usually **cheaper and cleaner**
than a restore. Back up when you'd lose something a rebuild can't recreate: the **retrieval
ledger** (`retrieval_event` — the audit trail of every agent call, ADR-0021) and **traces**
(`trace_span`) have no source to rebuild from — once gone, that history is gone. Back up before
any operation that touches those (a migration downgrade, a manual `DELETE`, an OS/Postgres
upgrade), or on a cadence if you rely on the ledger for audit/compliance.

### Take a backup

Custom format (`-Fc`) is the right choice for this database: compressed, supports
selective/parallel restore, and is `pg_restore`'s native input — never use plain-SQL `-Fp` for a
database you intend to `pg_restore` back in.

```sh
mkdir -p ~/pg-backups/agentic-kb-platform
pg_dump -Fc -d agentic_kb -f ~/pg-backups/agentic-kb-platform/agentic_kb_$(date +%Y%m%d_%H%M%S).dump
```

Put backups **outside the repo checkout** (e.g. `~/pg-backups/...` above) — there is no
`.gitignore` entry for dump files, and they can be tens of megabytes. Add `-h`/`-p`/`-U` flags if
you're not using the plain `psql agentic_kb` connection form from §1.

What you should see: the command exits 0 with no output, and the file exists —
`ls -lh ~/pg-backups/agentic-kb-platform/` shows it (a fully built local KB is a few MB; expect
low tens of MB, not more).

### Verify the dump before you trust it

```sh
pg_restore -l ~/pg-backups/agentic-kb-platform/agentic_kb_<timestamp>.dump | head -20
```

What you should see: a header block (`Archive created at ...`, `Format: CUSTOM`, `TOC Entries:
NN`) followed by one line per table/view/index/constraint. If this command errors, the dump is
corrupt or incomplete — redo the `pg_dump`.

### Restore into a fresh database (never over an existing one)

Always restore into a **new, empty** database — `pg_restore` is not designed to merge into an
already-populated schema. Use a throwaway name; never point this at a database you care about:

```sh
createdb agentic_kb_restore_check
pg_restore -d agentic_kb_restore_check ~/pg-backups/agentic-kb-platform/agentic_kb_<timestamp>.dump
```

Add `--no-owner --no-acl` if you're restoring on a machine/role that doesn't match the one the
dump was taken on (e.g. moving a dump from your laptop's `$USER` role to CI's `postgres` role):

```sh
pg_restore -d agentic_kb_restore_check --no-owner --no-acl ~/pg-backups/.../agentic_kb_<timestamp>.dump
```

### Confidence check: row counts before vs. after

```sh
for tbl in source_item knowledge_artifact knowledge_edge kb_build_run retrieval_event trace_span \
           generation_cache embedding_cache doc_extraction_output embedding_output \
           relationship_candidate relationship_judgment_cache entailment_cache generation_cache_artifact; do
  orig=$(psql -d agentic_kb -tAc "SELECT count(*) FROM $tbl;")
  restored=$(psql -d agentic_kb_restore_check -tAc "SELECT count(*) FROM $tbl;")
  printf "%-30s orig=%-8s restored=%-8s %s\n" "$tbl" "$orig" "$restored" \
    "$([ "$orig" = "$restored" ] && echo OK || echo MISMATCH)"
done
```

What you should see — every row `OK`, e.g.:

```
source_item                    orig=560      restored=560      OK
knowledge_artifact             orig=6254     restored=6254     OK
knowledge_edge                 orig=18176    restored=18176    OK
kb_build_run                   orig=1        restored=1        OK
retrieval_event                orig=140      restored=140      OK
trace_span                     orig=377      restored=377      OK
```

Also spot-check that the migration version and the four views came back:

```sh
psql -d agentic_kb_restore_check -c "SELECT * FROM alembic_version;"   # expect: 0021
psql -d agentic_kb_restore_check -c "\dv"                              # expect: the 4 v_* views
```

Once you've confirmed it, drop the throwaway:

```sh
dropdb agentic_kb_restore_check
```

To actually recover a lost/corrupted database, the same `pg_restore` step targets your real
database name instead of a throwaway one — but **create it fresh first** (§4's drop+recreate),
never restore on top of a database that already has tables.

### What NOT to back up

Test and scratch databases (`agentic_kb_test`, anything with `_test`/`_verify`/`_proof`/a
one-off suffix in the name). They hold no unique data — `agentic_kb_test`'s schema is torn down
and rebuilt by every test run (§7), and scratch databases are, by definition, disposable
experiments. Backing them up wastes disk and creates false confidence that they're durable state.

---

## 4. Reset / start over

The safe sequence to blow away a database and rebuild clean. This is destructive — double-check
the database name before running `dropdb`.

```sh
# 1. Make sure nothing is using it (skip straight to step 2 if you're sure nothing's connected)
dropdb agentic_kb   # if this fails with "being accessed by other users", see below

# 2. Recreate + migrate to head
createdb agentic_kb
cd services/kb-builder
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" uv run alembic upgrade head
cd ../..

# 3. Rebuild (zero-LLM, code + commits + aliases only — see dev-guide 22 §4 for a doc-inclusive build)
cd services/kb-builder
uv run python -m agentic_kb_builder.build --workspace ../.. --sources ./sources.example.yaml
cd ../..
```

What you should see at step 2: a scroll of `Running upgrade ... -> ...` lines ending at `0021`
(21 lines — one per migration). At step 3: the build CLI's standard tail,
`build status : active`.

### "database is being accessed by other users"

`dropdb`/`DROP DATABASE` refuses while any session — including a `psql` shell you left open in
another terminal, or a hung MCP server/build process — is connected. Find and end those sessions:

```sql
-- from a DIFFERENT database (you can't query pg_stat_activity while connected to the one you're dropping)
\c postgres
SELECT pid, usename, state, query, now() - query_start AS duration
FROM pg_stat_activity
WHERE datname = 'agentic_kb' AND pid <> pg_backend_pid();
```

What you should see: one row per other connection, with its running query and how long it's been
open. Then terminate them:

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'agentic_kb' AND pid <> pg_backend_pid();
```

Each terminated backend returns `t`. Re-run `dropdb agentic_kb` — it now succeeds.

### The single-builder advisory lock

Only **one** `build` process may write to a given registry at a time — enforced by a Postgres
session-level advisory lock the build takes before doing anything
(`services/kb-builder/src/agentic_kb_builder/application/builder_lock.py`). If a second build (or
a hung one) already holds it, the new one **aborts immediately** — it never queues:

```
event=builder_lock_held reason=another_builder_is_running
build aborted: another builder is running
```

This is not a gate and not a database problem — it's an operational fence. Fix: let the other
build finish, or find and kill its process (it releases the lock on exit, including a crash,
since the lock lives on a dedicated connection released in a `finally`). You can see whether the
lock is currently held (empty result = no build is running):

```sql
SELECT pid, granted FROM pg_locks WHERE locktype = 'advisory';
```

If you truly need to force-clear a stuck lock without finding the holder's PID (should be rare —
prefer `pg_terminate_backend` on the specific PID from `pg_locks` above), the lock is
session-scoped, so terminating its holding backend always releases it; there's no separate
"unlock" step needed once the process is gone.

---

## 5. Useful queries cookbook

**Tokens by agent and day** (ledger-derived cost view — see also `make dashboard`,
[06 — Observability](06-observability.md)):

```sql
SELECT date_trunc('day', created_at)::date AS day,
       agent_name,
       count(*)                          AS calls,
       sum(coalesce(tokens_returned, 0)) AS tokens
FROM retrieval_event
GROUP BY 1, 2
ORDER BY 1 DESC, 4 DESC;
```

**Last 20 retrievals, with status:**

```sql
SELECT tool_name, agent_name, status, tokens_returned, created_at
FROM retrieval_event ORDER BY created_at DESC LIMIT 20;
```

**Denials and errors** (a `denied` row is a contractual "budget said no", not a bug; an `error`
row is a failed call — both are first-class ledger outcomes, never silent):

```sql
SELECT tool_name, agent_name, status, created_at
FROM retrieval_event WHERE status IN ('denied', 'error') ORDER BY created_at DESC LIMIT 20;
```

**The zero-result KB-gap proxy.** The dashboard view does this for you per day
(`v_retrieval_health.kb_search_zero_thin_rate` — the ADR-0025 KB-gap signal: an *answered*
`kb_search` call that returned ≤ 1 artifact):

```sql
SELECT day, kb_search_answered, kb_search_zero_thin, kb_search_zero_thin_rate
FROM v_retrieval_health ORDER BY day DESC LIMIT 7;
```

To see the underlying rows instead of the daily rate:

```sql
SELECT tool_name, agent_name, coalesce(cardinality(returned_artifact_ids), 0) AS n_results, created_at
FROM retrieval_event
WHERE tool_name = 'kb_search' AND status = 'approved'
  AND coalesce(cardinality(returned_artifact_ids), 0) <= 1
ORDER BY created_at DESC LIMIT 20;
```

Zero rows back is a *good* sign — no gaps in that window, not a broken query.

**Slowest trace spans, by node** (which step of `get_task_context` costs the most time):

```sql
SELECT name,
       count(*)                                                     AS spans,
       round(avg(extract(epoch FROM ended_at - started_at)) * 1000)  AS avg_ms,
       round(max(extract(epoch FROM ended_at - started_at)) * 1000)  AS max_ms
FROM trace_span
WHERE started_at > now() - interval '7 days'
GROUP BY name
ORDER BY avg_ms DESC;
```

**Every span of one trace** (the step-by-step timeline of a single call — grab a `trace_id` from
a root span first, `parent_span_id IS NULL`):

```sql
SELECT trace_id, name, status, started_at FROM trace_span
WHERE parent_span_id IS NULL ORDER BY started_at DESC LIMIT 10;

SELECT name, status, round(extract(epoch FROM ended_at - started_at) * 1000) AS ms, started_at
FROM trace_span WHERE trace_id = '<trace-id-from-above>' ORDER BY started_at;
```

**Per-source-type freshness** — when a source type was last (re-)fetched:

```sql
SELECT source_type, count(*) AS sources, max(last_seen_at) AS most_recent, min(last_seen_at) AS oldest
FROM source_item WHERE NOT is_deleted GROUP BY 1 ORDER BY 1;
```

**Alias index size + sample** (the deterministic alias/reference miner, PR-38 —
[`docs/contracts/alias-reference.md`](../contracts/alias-reference.md)):

```sql
SELECT count(*) FROM knowledge_artifact WHERE artifact_type = 'alias_reference';

SELECT title, body_text::json ->> 'confirmation_count' AS confirmations
FROM knowledge_artifact WHERE artifact_type = 'alias_reference'
ORDER BY (body_text::json ->> 'confirmation_count')::int DESC NULLS LAST
LIMIT 10;
```

**Provenance checks** — every source should carry a `repo` (this repo's build always does; a
production GitHub/ADO build should too):

```sql
SELECT source_type, count(*) FROM source_item WHERE repo IS NULL GROUP BY 1;
```

Zero rows back is healthy. A nonzero count names the source type that's missing provenance —
worth investigating before you trust artifacts derived from it.

**`review_panel` drafts list.** The review-panel service owns a **separate schema**
(`review_panel`, not `public`) inside whatever database `REVIEW_PANEL_DATABASE_URL` points at —
often the same local Postgres instance, but check your env before assuming it's the same database
as your KB registry (`docs/contracts/review-panel.md`). It bootstraps itself on first use
(`CREATE SCHEMA IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS`), so it won't exist until
`review-panel` has run at least once. Connect to that database and list drafts:

```sql
SELECT draft_key, repo, pr_number, head_sha, created_at
FROM review_panel.review_draft
ORDER BY created_at DESC LIMIT 20;
```

`draft_key` is `<repo>#<pr_number>@<head_sha>` — at most one row per key (first-writer-wins,
idempotent). The full `draft` JSONB column holds the `review_draft_v1` document
(findings, verdicts, `summary_markdown`); see the contract for its shape. This schema is a
documented Alembic exemption — its own rollback is simply `DROP SCHEMA review_panel CASCADE`, and
it never shares a database transaction with the Knowledge Registry.

---

## 6. Maintenance

**Prune old trace spans.** `trace_span` is pure derived observability — "safe to prune by age or
drop entirely" by design (migration `0021`'s own docstring) — unlike `retrieval_event`, which is
the audit ledger and should be treated as **never** pruned without an explicit retention decision.
Preview, then delete:

```sql
-- dry run: how many would go?
SELECT count(*) FROM trace_span WHERE started_at < now() - interval '90 days';

-- the actual prune
DELETE FROM trace_span WHERE started_at < now() - interval '90 days';
```

What you should see: `DELETE N`. `retrieval_event`, `kb_build_run`, and every `knowledge_*` table
are the durable/rebuildable-from-source registry — don't run ad hoc `DELETE`s against them outside
the build engine's own reconcile/invalidation logic (which already handles supersession via
`valid_from_seq`/`invalidated_at_seq`, never a hard delete).

**Database size by table:**

```sql
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 15;
```

Whole-database size: `SELECT pg_size_pretty(pg_database_size('agentic_kb'));`

**Vacuum.** Postgres's autovacuum daemon is on by default and handles routine bloat/statistics for
this workload — you do not need a manual `VACUUM` in normal operation. The one time to run one by
hand is right after a large bulk delete (e.g. the trace-span prune above, or a big migration
downgrade), so the planner's statistics catch up immediately instead of waiting for autovacuum's
next pass:

```sql
VACUUM ANALYZE trace_span;
```

**Migration status.**

```sh
cd services/kb-builder
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" uv run alembic current
```

What you should see: `0021 (head)` when up to date. Anything else (an older revision number, or no
output) means migrations are behind — bring it current:

```sh
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" uv run alembic upgrade head
```

---

## 7. Test-database quirks

`agentic_kb_test` is **shared** across services and gets its schema **downgraded to base and
re-migrated to head** by kb-builder's own DB-backed integration tests on every run (every
migration's downgrade is exercised on every `make test-kb-builder`/`make verify-kb-builder`). Two
consequences:

- Never treat it as a place to browse or keep data — anything in it can vanish the next time
  someone runs the kb-builder test suite.
- Running mcp-server's or evals' DB-backed tests **right after** a kb-builder test run can fail
  with hundreds of "relation does not exist" errors, because the shared database was just
  downgraded to an empty schema and not yet re-migrated.

The Makefile self-heals this for you: `make test-mcp-server` and `make test-evals` both depend on
`make migrate-test-db`, which re-runs `alembic upgrade head` against `TEST_DATABASE_URL` before
the suite starts. If you're driving things by hand instead of through `make`, run that migration
yourself first:

```sh
cd services/kb-builder
DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head
```

Full explanation and the exact Makefile dependency:
[22 — Testing and builds](22-testing-and-builds.md) ("The verify gate" and "How DB tests work").
