# Command-line reference

Every command surface the platform ships: the bootstrap script, the build CLI, the server, the
review-panel CLI, the make targets, and the utility scripts.

## `./scripts/bootstrap.sh`

Fresh-machine onboarding: dependencies → migrated database → an active, verified knowledge base.
Idempotent and safely re-runnable.

| Flag / variable | Meaning |
|---|---|
| `--db-name <name>` (or `DB_NAME=`) | Database to create/build into (default `agentic_kb`). |
| `--with-docs` | After the default build, run a second incremental build that adds doc summaries. Needs `LLM_PROVIDER` + `LLM_API_KEY` (read from `.env`); if the pass fails, the zero-LLM KB stays active. |
| `-h`, `--help` | Usage. |
| `PGHOST` / `PGPORT` / `PGUSER` | Postgres connection overrides (defaults `localhost` / `5432` / `$USER`). |
| `MCP_PORT` | Port used in the printed serve command (default `8765`). |

The five stages: preflight (tools + Postgres reachability) → `uv sync` for all four projects →
create + migrate the database → the zero-LLM build (code + commits + aliases) → smoke-verify (an
active `kb_version` + the 25-query alias-resolution retrieval check).

## The build CLI

```sh
cd services/kb-builder
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run python -m agentic_kb_builder.build --workspace ../.. --sources ../../scripts/local-code-sources.yaml
```

| Flag | Effect |
|---|---|
| `--sources <yaml>` | **Required.** Path to a sources.yaml describing what to index. |
| `--workspace <dir>` | **Required.** Workspace root the local-FS backend reads. |
| `--backend {local,production}` | `local` (default) reads `--workspace` files; `production` fetches real GitHub/ADO sources. The local backend can only fetch `github_code`/`github_doc`; other source types are skipped with a warning. |
| `--kb-version <label>` | Version label (default `local.<UTC timestamp>`). |
| `--version <sha>` | `source_version` stamp for local files (default `local`). |
| `--validate-only` | Run the config pre-flight (auth/tokens/paths for the chosen backend) and exit without building — no database or network access. Prints `config ok` (exit 0) or the errors (exit 1). |
| `--no-activate` | Build but do not mark the version active. |
| `--no-git-metadata` | Skip turning local git commits into `commit` artifacts. Use when indexing a remote repo from this checkout. |
| `--allow-large-delta` | Override the symbol-count-delta publish gate for an intentional large change (recorded and logged). **No other gate is overridable.** |
| `--index-path <file>` | Persistent local search index (default `$KB_LOCAL_INDEX_PATH` or `./.kb-local-search-index.json`). A rebuildable projection of Postgres — delete it to force a clean reprojection. |
| `--log-format {timeline,raw,json}` | Terminal log rendering (TTY default `timeline`, non-TTY `raw`). Overrides `$LOG_FORMAT`. |

Successful tail:

```
build status : active
kb_version   : local.<timestamp>
active version: local.<timestamp>
search index : .kb-local-search-index.json
```

Only one builder may write a registry at a time (a Postgres advisory lock); a second builder
aborts immediately with `build aborted: another builder is running`.

## Migrations (Alembic, kb-builder owns the schema)

```sh
cd services/kb-builder
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"
uv run alembic upgrade head     # apply everything (head: 0023)
uv run alembic current          # -> 0023 (head)
uv run alembic downgrade -1     # roll back one revision (verify a new migration's rollback)
```

The MCP server never runs migrations.

## The Obsidian export

Browse the knowledge graph as linked Markdown notes (one note per artifact, `[[wikilinks]]` for
edges, foldered by type):

```sh
cd services/kb-builder
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run python -m agentic_kb_builder.export_obsidian --out ./vault
```

| Flag | Effect |
|---|---|
| `--out <dir>` | **Required.** Output directory for the vault. |
| `--kb-version <label>` | Export a specific version (default: the active one). |

Re-running is deterministic (stable slugs) and overwrites the folder cleanly.

## The MCP server

```sh
cd services/mcp-server
MCP_LOCAL_DEV_AUTH=1 MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765 \
MCP_ENTRA_TENANT_ID=local-dev MCP_ENTRA_AUDIENCE=api://local MCP_LOCAL_DEV_TEAMS=platform \
MCP_AGENT_ALLOWANCES='{"local-dev": {"max_requests": 50, "max_tokens": 50000}}' \
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run python -m agentic_mcp_server
```

Configuration is environment-only — see
[environment-variables.md](environment-variables.md). `GET /health` is the unauthenticated
readiness probe: `{"status":"ok", ...}` when serving, 503 `no_active_kb_version` or
`registry_unreachable` otherwise.

### The replay CLI

```sh
cd services/mcp-server
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run python -m agentic_mcp_server.replay <run_id>
```

Prints a run's retrieval-ledger rows as a human-readable timeline (one line per action:
time, elapsed, tool, outcome, expanded `details`). Exit codes: 0 = printed, 1 = no rows,
2 = bad args. An operator tool — connects directly to `DATABASE_URL`, no broker needed.
Note: `kb_search` is session-scoped, not run-scoped — its rows use the `run_id = "-"` sentinel,
so use SQL for those (see [database.md](database.md)).

## The review-panel CLI

```sh
./scripts/run_review_panel_local.sh <owner>/<repo> <pr-number>   # wrapper: sources .env, sets REVIEW_PANEL_AGENTS_DIR

cd services/review-panel                                          # or the CLI directly
uv run review-panel draft <owner>/<repo> <pr-number>
```

One subcommand: `draft`. Behavior is contractual: a stored draft for the PR's current head SHA is
printed with **no model calls**; otherwise the draft is computed (LLM credentials required),
stored, and printed. **stdout carries only the `review_draft_v1` JSON; all logs go to stderr** —
piping into `jq` is safe. Exit code 0 on success, 1 on failure.

## Make targets

| Target | What it does |
|---|---|
| `make sync` | `uv sync` for kb-builder, mcp-server, review-panel, and evals (also `sync-<project>`). |
| `make lint` / `make types` / `make test` | ruff / pyright / pytest across all four projects (also per-project variants). |
| `make verify` | lint + types + tests for everything — the definition of "done" (also `verify-<project>`). |
| `make migrate-test-db` | kb-builder's Alembic migrations against `TEST_DATABASE_URL`. `test-mcp-server` and `test-evals` depend on it because kb-builder's own suite downgrades the shared test DB to base on teardown. |
| `make eval-run` | The golden-query retrieval evals against the migrated test registry. |
| `make eval-all` | The consolidated tiered evaluation (T1–T4); tiers missing prerequisites skip with a stated reason. `--with-gates` (via `run_all.py`) adds T0 = `make verify`. |
| `make dashboard` | The read-only operator dashboard from the `v_*` views → `evals/dashboard.html` + `.md`. Reads `DATABASE_URL` from your shell, else `TEST_DATABASE_URL`. |
| `make demo` | `scripts/e2e-local.sh` — a hermetic local end-to-end demo (Postgres + uv only). |

Default `TEST_DATABASE_URL` assumes a `postgres:postgres` role (CI). Homebrew users:

```sh
make verify TEST_DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test"
```

## Utility scripts

| Script | One line |
|---|---|
| `scripts/smoke_client.py` | Drives the governed evidence-pack flow end to end against a running broker (`MCP_URL=http://127.0.0.1:8765/mcp/ uv run --project services/mcp-server python scripts/smoke_client.py`); prints what each step proves, ends `smoke passed`. |
| `scripts/eval_alias_resolution.py` | The zero-LLM alias-resolution retrieval check bootstrap runs (25 real queries against the live `alias_reference` table; asserts ≥ 80% top-1). Needs `DATABASE_URL`. |
| `scripts/eval_task_context.py` | The `get_task_context` two-arm A/B evaluation over realistic dev tasks. |
| `scripts/agent_runner.py` | The terminal multi-agent runner over the governed lanes: EXPLAIN answers with cited sources; change tasks pause for your approval at every hand-off (`--auto-approve` to run unattended). Prints a `run_id` + replay command. |
| `scripts/kb_agent.py` | A single KB-first terminal agent (needs `LLM_*`; loads `.env` itself). |
| `scripts/codeskeleton.py` | The deterministic skeleton-first code reader (signatures kept, bodies elided; ADR-0026). |
| `scripts/integration/run_opencode.sh` | Drives the committed OpenCode rendering against the real broker for one discipline case (`OPENCODE_MODEL=<groq-model-id> scripts/integration/run_opencode.sh <case-id>`). |
| `scripts/integration/run_copilot.sh` | Same, for the Copilot CLI rendering. |
| `agents/check_parity.py` | Stdlib-only parity check between `agents/` and the `.copilot/`/`.opencode/` renderings (exit 0 = clean). |
