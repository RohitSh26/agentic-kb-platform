# Rebuild after changes

**Goal:** keep the knowledge base current as code changes — cheaply, because the build only
reprocesses what actually changed.

## The everyday path: just re-run the build

```sh
./scripts/bootstrap.sh          # skips deps/DB that already exist, rebuilds incrementally
```

The build is incremental at every level: a source whose `content_hash` is unchanged is skipped
entirely (no chunking, no LLM, no embedding), and the generation and embedding caches gate every
model call for the sources that did change.

**Verify:** on a rebuild with few or no changes, expect `event=build_skip_unchanged` for most
sources and near-zero `llm_calls` in the `event=build_run_completed` line, ending in the standard
tail:

```
build status : active
kb_version   : local.<timestamp>
active version: local.<timestamp>
search index : .kb-local-search-index.json
```

The newly activated version still serves the **complete** knowledge set, not just the day's delta
— version membership is interval-based, so unchanged artifacts carry forward automatically.

## The local search index file

`.kb-local-search-index.json` is a persistent, derived, rebuildable projection of Postgres —
never truth. It carries forward across incremental rebuilds and self-heals: after a database
recreate, the first rebuild's orphan sweep removes stale entries and re-projects fresh ones. You
never need to manage it in normal use. To force a fully clean projection, delete the file before
rebuilding.

## The one-time fresh rebuild (provenance check)

There is exactly one situation an incremental rebuild cannot fix: a database first built before
the builder stamped source provenance. Such databases carry `source_item` rows whose identity
columns (`repo`, `branch`, `external_id`) are `NULL`. The builder heals these whenever a source's
*content* changes, but a source that never changes again keeps its stale row forever. Check yours:

```sh
psql agentic_kb -c "select count(*) from source_item
                    where repo is null and source_type in ('github_code','github_doc');"
```

- `0` — nothing to do (any recently built database is fine).
- Non-zero — do a one-time fresh rebuild:

  ```sh
  pkill -f agentic_mcp_server          # stop the server so dropdb isn't blocked
  dropdb --force agentic_kb
  ./scripts/bootstrap.sh
  ```

  Leave `.kb-local-search-index.json` in place — the first rebuild self-heals it.

**Verify:** the check returns `0` and the rebuild tail shows `build status : active`.

## If a second build refuses to start

Only one build may write to a registry at a time — a Postgres advisory lock, taken before any
work. A second builder aborts immediately (it never queues):

```
event=builder_lock_held reason=another_builder_is_running
build aborted: another builder is running
```

Let the other build finish, or kill its process — the lock is released on exit, including a
crash. More: [troubleshooting](troubleshoot.md), "Builder lock held".

Indexing your own repositories instead of this checkout:
[index your own repositories](index-your-own-sources.md). Wiping and starting over:
[reset the database](reset-the-database.md).
