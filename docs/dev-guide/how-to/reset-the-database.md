# Reset the database

**Goal:** blow away a knowledge base and rebuild clean. This is destructive — double-check the
database name before running `dropdb`.

## Steps

1. **Drop it** (stop anything using it first — see below if this fails):

   ```sh
   dropdb agentic_kb
   ```

2. **Recreate and migrate to head:**

   ```sh
   createdb agentic_kb
   cd services/kb-builder
   DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" uv run alembic upgrade head
   cd ../..
   ```

   **Verify:** a scroll of `Running upgrade ... -> ...` lines ending at `0023`.

3. **Rebuild:**

   ```sh
   ./scripts/bootstrap.sh
   ```

   (Or the build CLI directly — see [the CLI reference](../reference/cli.md).)

   **Verify:** the build tail prints `build status : active`.

## "database is being accessed by other users"

`dropdb` refuses while any session is connected — a `psql` shell left open in another terminal, or
a running MCP server or build. The quick fixes:

```sh
pkill -f agentic_mcp_server     # stop the server
dropdb --force agentic_kb       # or force-drop, terminating connections
```

To see who is connected first, query from a **different** database (you can't inspect the one you
are dropping from inside it):

```sql
\c postgres
SELECT pid, usename, state, query, now() - query_start AS duration
FROM pg_stat_activity
WHERE datname = 'agentic_kb' AND pid <> pg_backend_pid();
```

Then terminate them (each returns `t`):

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'agentic_kb' AND pid <> pg_backend_pid();
```

Re-run `dropdb agentic_kb` — it now succeeds.

## The single-builder advisory lock

Only one build process may write to a registry at a time — a Postgres session-level advisory lock
taken before any work. A second build aborts immediately with
`build aborted: another builder is running`; it never queues. See whether the lock is currently
held (empty result = no build running):

```sql
SELECT pid, granted FROM pg_locks WHERE locktype = 'advisory';
```

The lock is session-scoped: terminating the holding backend (or letting the build finish — it
releases on exit, including a crash) always releases it. There is no separate unlock step.

Restoring from a backup instead of rebuilding: [back up and restore](back-up-and-restore.md).
