# 08 — Troubleshooting

Every known failure mode, in one place, organized by symptom: what you see → why → the exact fix →
where to read more. If a command from the other pages didn't behave as described, look it up here
first.

## Install and bootstrap

| Symptom | Cause | Fix |
|---|---|---|
| `command not found` right after installing a tool | The installer updated your shell profile, which only new terminals pick up | Open a **new** terminal and retry |
| `Postgres isn't reachable at localhost:5432` (bootstrap preflight) | Postgres isn't running, or runs on a non-default host/port | `brew services start postgresql@16` (macOS) or start your Docker Postgres. Non-default port/host: `PGHOST=... PGPORT=... ./scripts/bootstrap.sh` |
| `uv sync` fails partway | Transient download/build failure | Re-run `./scripts/bootstrap.sh` — `uv sync` is idempotent. Clean retry: `rm -rf services/<project>/.venv && ./scripts/bootstrap.sh` |
| `no module named agentic_kb_builder` | Wrong directory, or dependencies not installed | Run build commands from `services/kb-builder`; re-run `make sync` from the repo root. On an old checkout: `git pull` |

More: [01 — Run the platform](01-run-the-platform.md).

## Database

| Symptom | Cause | Fix |
|---|---|---|
| `createdb: command not found` | Postgres client tools not on PATH | `brew services start postgresql@16`, then open a new terminal |
| `createdb failed` / `role "postgres" does not exist` / `role "<you>" does not exist` | Homebrew Postgres names its superuser after your macOS user; CI/Docker Postgres use `postgres:postgres` | Use the role your Postgres actually has: `PGUSER=<role> ./scripts/bootstrap.sh`, or put it in the URL (`postgresql+asyncpg://postgres@localhost:5432/agentic_kb`) and `createdb -U postgres agentic_kb` |
| `database "agentic_kb" already exists` | It's already there — not an error | Skip `createdb`; to start clean: `dropdb agentic_kb && createdb agentic_kb` |
| `must use the asyncpg driver` | Wrong URL scheme | `DATABASE_URL` must start with `postgresql+asyncpg://` exactly — everything is async SQLAlchemy |
| **`database "agentic_kb" is being accessed by other users`** on `dropdb` | The MCP server (or a psql session) still holds connections | Stop the server first — `pkill -f agentic_mcp_server` — then `dropdb agentic_kb`; or force it: `dropdb --force agentic_kb` |
| `relation "knowledge_artifact" does not exist` | Wrong database, or migrations not applied | `psql agentic_kb` (not the test DB), then `cd services/kb-builder && uv run alembic upgrade head` |
| `alembic upgrade head` complains about a **downgraded/missing** table after running `make test-kb-builder` / `make verify` against the same database | kb-builder's test suite migrates up **and down to base** on teardown — it owns a disposable test database, not your browsing/serving one | Keep them separate: bootstrap's `agentic_kb` for browsing/serving; `agentic_kb_test` + `TEST_DATABASE_URL` for `make verify`/`make test-*`. The self-heal is automatic: `make test-mcp-server` / `make test-evals` depend on `make migrate-test-db`, which re-migrates the test DB to head before each suite |

More: [05 — Database operations](05-database-operations.md),
[22 — Testing and builds](22-testing-and-builds.md).

## Build failed a gate (or didn't activate)

| Symptom | Cause | Fix |
|---|---|---|
| `build status` is `validation_failed` (not `active`) | A publish gate blocked activation — the previous active version keeps serving, nothing breaks | Read the log tail for `event=publish_gate_failed gate=<name>`, or query the run row: `psql agentic_kb -c "select kb_version, status, failed_gate, gate_measured_value, error_summary from kb_build_run where status in ('failed','validation_failed') order by build_seq desc limit 3;"` |
| Build not `active` and the DB looks empty | The database wasn't migrated before the build | Re-run `./scripts/bootstrap.sh` (it migrates, then rebuilds), or migrate by hand: `cd services/kb-builder && uv run alembic upgrade head` |
| `event=index_drift class=missing … count=N` then `publish_gate_failed gate=index_consistency` | A stale or deleted local search-index file paired with a freshly built database | Rebuild both from scratch: drop the DB **and** delete `.kb-local-search-index.json`, then rebuild — the gate clears |
| Symbol-count-delta gate fails on a first build or a big refactor | The delta gate is doing its job on an unusual-but-legitimate build | Re-run with `--allow-large-delta` — the **only** overridable gate; the override is recorded on `kb_build_run` and logged |
| A handful of `event=build_source_failed` lines, but the build still reports `active` | Expected — each source commits independently; one source's failure never aborts the build | Nothing, unless the `extractor_error_rate` gate fails the version (>1% of sources) — then investigate the named sources |
| `event=docify_mapped … source_backed=N interpreted=M` | Normal classification, not an error — verbatim quotes became citable facts, paraphrases stayed interpreted | Nothing. Fewer source-backed facts with a small model is expected |
| Artifact/edge counts come back `0` | The build didn't activate, or you're pointed at the wrong DB | Confirm `DATABASE_URL` ends in `/agentic_kb` and the build printed `build status : active` |
| `Migrations behind` / build errors naming a missing column | Out-of-date schema | `cd services/kb-builder && uv run alembic upgrade head` |

More: [06 — Observability](06-observability.md) §"What a gate-blocked build looks like".

## Builder lock held

| Symptom | Cause | Fix |
|---|---|---|
| `build aborted: another builder is running` (`event=builder_lock_held`) | The single-builder Postgres **advisory lock**: another build — possibly hung — holds this registry. The CLI exits immediately (exit 1) rather than queueing | Wait for the other build to finish, or kill its process, then re-run — nothing to clean up. The lock is released on normal exit *and* on crash |

## Server won't start / health checks fail

| Symptom | Cause | Fix |
|---|---|---|
| Server exits with `missing required environment variables: DATABASE_URL, ...` | The three required vars aren't all set | Copy the whole command block from [01](01-run-the-platform.md) — all the `MCP_*` variables belong on the same command line before `uv run` |
| `curl /health` returns nothing / connection refused | The server didn't start, or is bound elsewhere | Read the server terminal for the real error (most often a bad `DATABASE_URL`); check `MCP_HTTP_PORT` / `MCP_HOST_PORT` |
| `/health` → **503 `no_active_kb_version`** | The server is up but the registry has no active KB — readiness honesty, not a crash | Build one: `./scripts/bootstrap.sh`, and confirm `build status : active`. On a fresh compose volume this is the expected state until you run the build profile |
| `/health` → **503 `registry_unreachable`** | The server can't reach Postgres | Check `DATABASE_URL`, the network/tunnel, and that the database is up |
| Server refuses to boot with local-dev auth | Guardrails: a real tenant id or a non-loopback bind with `MCP_LOCAL_DEV_AUTH=1` | Use `MCP_ENTRA_TENANT_ID=local-dev` and `MCP_HTTP_HOST=127.0.0.1`, exactly as in [01](01-run-the-platform.md) |
| A tool reports a stale/wrong `kb_version` | The server serves the single `active` row; a newer build may not have activated | Check `psql agentic_kb -c "select kb_version, status from kb_build_run order by build_seq desc limit 3;"` — if the newest run isn't `active`, see §"Build failed a gate" above |

## Port already in use

| Symptom | Cause | Fix |
|---|---|---|
| `address already in use` on `:8765` (or `:8000`) | An old server instance is still running | `pkill -f agentic_mcp_server`, then start again — or start on a different port (`MCP_HTTP_PORT=...`) and update the client config's URL to match |

## Editor doesn't see the tools

| Symptom | Cause | Fix |
|---|---|---|
| VS Code: `context-broker` won't start / shows red | The broker isn't running yet | Start it ([01](01-run-the-platform.md)); confirm `curl http://127.0.0.1:8765/health` → `ok`; click **Start** again |
| VS Code: no tools show up in the chat | Wrong chat mode, or the server isn't enabled in the tool picker | Switch the mode dropdown to **Agent** (not *Ask*/*Edit*); enable `context-broker` in the tools picker; `Cmd-Shift-P` → *Reload Window* if needed |
| VS Code: no Start link in `mcp.json` | Code-lens UI variance | `Cmd-Shift-P` → **MCP: List Servers** → `context-broker` → Start |
| The model answers from general knowledge without calling tools | Models sometimes skip optional tools | Start the prompt with "Using the context-broker kb_search tool, …" / "search the KB before reading any file"; confirm the tools are enabled (VS Code picker / `copilot mcp list`) |
| Copilot CLI: auth fails | No Copilot license on the `gh` account, or a classic `ghp_` PAT in the token env vars (not accepted) | Use a Copilot-licensed account; `export GH_TOKEN="$(gh auth token)"` or `copilot login` (device flow). Beware an exported `GITHUB_TOKEN` from `.env` shadowing the keyring token |
| Copilot CLI: can't reach `context-broker` | Broker not running, or config missing | Start the broker; confirm the server appears in `copilot mcp list`; re-check `~/.copilot/mcp-config.json` ([02](02-connect-your-editor.md)) |
| Copilot CLI: more tools than `kb_search`/`get_task_context` show up | The server entry is missing its `tools` allowlist — it was added ad-hoc | Replace it with the committed block from [02](02-connect-your-editor.md) — never `copilot mcp add` |
| OpenCode: tools listed but never called / answers without citations | Weak host model — measured across four Groq free-tier models, none passed discipline | Configure a strong tool-calling provider/model, then spot-check: `OPENCODE_MODEL=<groq-model-id> scripts/integration/run_opencode.sh opencode-t4-explain-1` ([02](02-connect-your-editor.md) §OpenCode) |
| A tool call returns **401** | The broker isn't in local-dev mode / isn't on loopback, so the placeholder bearer is rejected — or a real token's `aud`/issuer don't match | Local: restart the server exactly as in [01](01-run-the-platform.md). Remote: acquire a token for `MCP_ENTRA_AUDIENCE` ([07](07-providers-and-api-keys.md) §Broker bearer tokens). There is no auth-off switch |

## Budget notices

| Symptom | Cause | Fix |
|---|---|---|
| Every `kb_search` returns *"KB budget spent — work with what you have…"* | The per-session dual cap (calls or tokens) closed — working as designed, ledgered as `denied`, never an error | Start a fresh session (new chat window), or raise `max_requests`/`max_tokens` in `MCP_AGENT_ALLOWANCES` for your subject and restart the broker. What the budget is for: [03](03-using-the-knowledge-tools.md) |
| Every call comes back `denied` in the ledger | Same cap, viewed from the ledger side | Same fix; `denied` rows are the budget doing its job, not a fault |

## Real-source fetches (`--backend production`)

| Symptom | Cause | Fix |
|---|---|---|
| One GitHub source `404`s while another (same repo) succeeds | That source is **missing its `auth:` block** — it ran unauthenticated, and a private repo returns 404. The error says "no Authorization header was sent" when this happens | Give every `github_*` / `azure_wiki` / `ado_card` source pointing at a private resource its own `auth: token_env: …` |
| GitHub/ADO **404** (auth present) | The token can't see the private resource (GitHub returns 404, not 403) | Classic PAT needs `repo` scope; a fine-grained PAT must be *granted to that repo*; an ADO PAT needs Wiki/Work-Item **Read** |
| **401** bad/expired credentials | Token wrong or expired | Re-create it and re-`source .env` in *this* shell |
| **403** | Missing scope, org SSO not authorized for the PAT, or a rate limit | Fix the scope / authorize SSO / wait out the limit |
| Build wants an LLM but none is set | Your sources include prose (doc/wiki/card) | Set `LLM_*` in `.env` ([07](07-providers-and-api-keys.md)), or index code only |
| `azure_wiki`/`ado_card` sources skipped under `--backend local` | `event=source_skipped_not_locally_fetchable` — the local backend can only read `github_code`/`github_doc` from your checkout. A skip, not an error | Use `--backend production` for ADO sources ([22](22-testing-and-builds.md) §7) |

## LLM providers

| Symptom | Cause | Fix |
|---|---|---|
| `LLM_API_KEY is required for provider ...` | A remote provider with no key — the build fails fast rather than pretending | `export LLM_API_KEY=...`, or use `LLM_PROVIDER=ollama` for the local, key-free fallback |
| `Connection refused` to `:11434` | You picked the Ollama fallback but it isn't running | `ollama serve` (and `ollama pull <model>` first) |
| Review panel / kb-builder rejects your provider name | The provider sets differ per component — review-panel accepts a narrower set (no `azure`, no `anthropic_foundry`) | Check the provider-acceptance matrix in [07 — Providers and API keys](07-providers-and-api-keys.md) |

---

Still stuck? The deeper references: [22 — Testing and builds](22-testing-and-builds.md)
(build internals, the full SQL health reference), [06 — Observability](06-observability.md)
(what the system recorded about the failure), and
[05 — Database operations](05-database-operations.md).
