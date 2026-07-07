# Back up and restore

**Goal:** protect the data a rebuild cannot recreate, and restore it with confidence.

**Do you actually need a backup?** The knowledge registry itself is rebuildable from its sources
— if all you would lose is derived knowledge (artifacts, edges, caches), a rebuild is usually
cheaper and cleaner than a restore. Back up when you would lose something a rebuild cannot
recreate: the **retrieval ledger** (`retrieval_event` — the audit trail of every agent call) and
**traces** (`trace_span`) have no source to rebuild from. Back up before any operation that
touches those (a migration downgrade, a manual `DELETE`, an OS/Postgres upgrade), or on a cadence
if you rely on the ledger for audit.

## Take a backup

Custom format (`-Fc`) is the right choice: compressed, supports selective/parallel restore, and
is `pg_restore`'s native input. Put backups outside the repo checkout — there is no `.gitignore`
entry for dump files:

```sh
mkdir -p ~/pg-backups/agentic-kb-platform
pg_dump -Fc -d agentic_kb \
  -f ~/pg-backups/agentic-kb-platform/agentic_kb_$(date +%Y%m%d_%H%M%S).dump
```

**Verify:** the command exits 0 with no output, and `ls -lh ~/pg-backups/agentic-kb-platform/`
shows the file (a fully built local KB is a few MB; expect low tens of MB at most). Add
`-h`/`-p`/`-U` flags if you don't use the plain socket connection.

## Verify the dump before you trust it

```sh
pg_restore -l ~/pg-backups/agentic-kb-platform/agentic_kb_<timestamp>.dump | head -20
```

**Verify:** a header block (`Archive created at ...`, `Format: CUSTOM`, `TOC Entries: NN`)
followed by one line per table/view/index/constraint. If this errors, the dump is corrupt or
incomplete — redo the `pg_dump`.

## Restore into a fresh database (never over an existing one)

`pg_restore` is not designed to merge into an already-populated schema. Always restore into a
new, empty database:

```sh
createdb agentic_kb_restore_check
pg_restore -d agentic_kb_restore_check \
  ~/pg-backups/agentic-kb-platform/agentic_kb_<timestamp>.dump
```

Add `--no-owner --no-acl` when restoring on a machine or role that doesn't match the one the dump
was taken on (e.g. moving from your laptop's `$USER` role to CI's `postgres` role).

## Confidence check: row counts before vs. after

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

**Verify:** every row `OK`, e.g.:

```
source_item                    orig=560      restored=560      OK
knowledge_artifact             orig=6254     restored=6254     OK
knowledge_edge                 orig=18176    restored=18176    OK
kb_build_run                   orig=1        restored=1        OK
retrieval_event                orig=140      restored=140      OK
trace_span                     orig=377      restored=377      OK
```

Spot-check that the migration version and the four views came back:

```sh
psql -d agentic_kb_restore_check -c "SELECT * FROM alembic_version;"   # expect: 0023
psql -d agentic_kb_restore_check -c "\dv"                              # expect: the 4 v_* views
```

Then drop the throwaway:

```sh
dropdb agentic_kb_restore_check
```

## Recovering for real

The same `pg_restore` step, targeting your real database name — but **create it fresh first**
([reset the database](reset-the-database.md) has the drop+recreate sequence). Never restore on
top of a database that already has tables.

## What NOT to back up

Test and scratch databases (`agentic_kb_test`, anything with a `_test`/`_verify`/`_proof`/one-off
suffix). They hold no unique data — the shared test database's schema is torn down and rebuilt by
every test run, and scratch databases are disposable experiments by definition.
