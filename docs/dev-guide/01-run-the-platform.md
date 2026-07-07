# 00 — Quickstart: one command from clone to a queryable KB

**The scripted 10-minute path.** One script (`scripts/bootstrap.sh`) takes a fresh clone to an
**active, queryable knowledge base** — synced dependencies, a migrated Postgres database, a real
zero-LLM build, and a real retrieval check proving it works. No cloud accounts, no API keys, no
tokens.

> Want the click-by-click, explained version instead (what each piece is and why), or you're not
> on macOS/Homebrew? See **[00-getting-started.md](00-getting-started.md)** — same destination,
> narrated by hand. This page is the fast path once you (or a script) can run a few shell commands.

---

## Prerequisites

| Tool | Why | Check | Install (macOS/Homebrew) |
|---|---|---|---|
| **git** | clone the repo | `git --version` | ships with Xcode CLT, or `brew install git` |
| **uv** | manages Python 3.12 + all four projects' dependencies | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (open a **new** terminal after) |
| **Postgres 16** | the Knowledge Registry — the one real piece of infrastructure | `pg_isready` | `brew install postgresql@16 && brew services start postgresql@16` |

That's it. Python 3.12 itself is **not** a separate prerequisite — `uv` fetches it automatically the
first time it's needed (`bootstrap.sh` checks for it and tells you either way, but only *fails* on
git/uv/Postgres being genuinely missing).

**No API key for the default build.** For doc summaries, cloud LLM providers (Groq/OpenAI/Azure
OpenAI/Claude on Azure AI Foundry), and every other key this platform can use, see
[11 — Providers and API keys](11-providers-and-api-keys.md).

## The one command

```sh
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform
./scripts/bootstrap.sh
```

Takes about 2-3 minutes on a machine that already has the three prerequisites (most of that is
`uv sync` downloading packages the first time). Safe to re-run: it skips whatever already exists
and the build itself is incremental.

Override the database name (the script defaults to `agentic_kb`) with a flag or an env var —
useful for a throwaway proof run:

```sh
./scripts/bootstrap.sh --db-name agentic_kb_bootstrap_proof
# or
DB_NAME=agentic_kb_bootstrap_proof ./scripts/bootstrap.sh
```

## What you should see, stage by stage

**1/5 Preflight** — a line per tool, ending `postgres — OK (reachable at localhost:5432)`. Any
failure here names the exact thing missing and the command to fix it; nothing later runs until
this passes.

**2/5 `uv sync`** — one block per project (`services/kb-builder`, `services/mcp-server`,
`services/review-panel`, `evals`), each ending with `uv`'s own "Resolved / Installed" summary and
no errors.

**3/5 Database** — either `database 'agentic_kb' already exists — skipping createdb` (a re-run) or
`created database 'agentic_kb'`, then a scroll of Alembic `Running upgrade ... -> ...` lines ending
in `schema is at head`.

**4/5 The build** — a scroll of structured `event=...` build-log lines (source upserts, cache
lookups, graphify/linker/alias-miner activity), ending in the same four lines the build CLI always
prints:

```
build status : active
kb_version   : local.20260705T124413Z
active version: local.20260705T124413Z
search index : .kb-local-search-index.json
```

`build status : active` is the one line that matters — it means the publish gates
(`docs/contracts/publish-gates.md`) passed and this version is now what the MCP server will serve.
This build is **code + commits + aliases only** (this repo's own Python source, plus one
deterministic `commit` artifact per local git commit, plus the PR-38 alias index) — zero LLM calls,
confirmed by `llm_calls=0` in the `event=build_run_completed` log line.

**5/5 Smoke-verify** — two checks, both real, neither `SELECT 1`:

```
active kb_version: local.20260705T124413Z
running the alias-resolution retrieval check (real kb_search-path code, zero LLM)...
HIT   alias-01-alias-reference-index    query='the alias reference index'   top1=docs/pr-briefs/PR-38-alias-reference-index.md
...
25/25 top-1 hits = 100.0%
PASS (target >= 80%)
```

The first check queries `kb_build_run` for an active version — a bootstrap with no active KB is a
failure, not a warning. The second (`scripts/eval_alias_resolution.py`) resolves 25 real natural-
language queries against the *live* `alias_reference` table your build just wrote — the same table
the MCP server's `kb_search` / `get_task_context` path reads
(`services/mcp-server/src/agentic_mcp_server/context_broker/task_context_nodes.py`) — and asserts
≥80% top-1 accuracy. This is meaningfully different from a bare connectivity check: it proves the
build produced *retrievable, resolvable* knowledge, not just rows in a table.

Then a **Next steps** block prints — the commands below are the same ones it shows you.

## Connect a host

The KB is only useful once something serves it. Start the MCP Context Broker:

```sh
cd services/mcp-server
MCP_LOCAL_DEV_AUTH=1 MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765 \
MCP_ENTRA_TENANT_ID=local-dev MCP_ENTRA_AUDIENCE=api://local MCP_LOCAL_DEV_TEAMS=platform \
MCP_AGENT_ALLOWANCES='{"local-dev": {"max_requests": 50, "max_tokens": 50000}}' \
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run python -m agentic_mcp_server
```

Verify: `curl -s http://127.0.0.1:8765/health` → `{"status":"ok", ..., "active_kb_version":"local...."}`.
Leave it running in this terminal; do the rest in a second one.

| Host | How | Where it's wired |
|---|---|---|
| **VS Code + GitHub Copilot** | Open the folder, open `.vscode/mcp.json`, click **Start** above the `context-broker` block, switch Copilot Chat to **Agent** mode. | Already shipped, already points at `http://127.0.0.1:8765/mcp/`. Nothing to edit for local use. |
| **OpenCode** | Copy `.opencode/` to your project root (or `~/.config/opencode/`), set the broker URL in `opencode.json` to `http://127.0.0.1:8765/mcp/`, `export CONTEXT_BROKER_TOKEN=anything` (ignored in local-dev mode). | `.opencode/README.md` — a host-native rendering of `agents/`, parity-pinned to the canon. |
| **GitHub Copilot CLI / cloud agent** | Repo settings → Copilot → MCP servers using `.copilot/mcp/repository-settings.json`, or VS Code via `.copilot/mcp/vscode-mcp.json` (functionally the same file as `.vscode/mcp.json`). | `.copilot/README.md`; see also [09-copilot-cli-end-to-end.md](09-copilot-cli-end-to-end.md). |

Full click-by-click version (screenshots-in-words, what to expect from each tool call): dev-guide
[00-getting-started.md](00-getting-started.md) Parts 5-8.

## Run the evals

```sh
make eval-all
```

Runs every tier that *can* run in your environment (T1-T4); tiers needing something you haven't
set up (`TEST_DATABASE_URL`, `DATABASE_URL`, LLM creds) **skip with a stated reason** rather than
failing or inventing a number. Add `--with-gates` (via `cd evals && uv run python run_all.py
--with-gates`) to also run the generic lint+types+tests gate (`make verify`). See
`docs/architecture/evaluation-system.md` for what each tier checks.

## Want doc/wiki/ticket summaries too?

The default build is **code-only, zero-LLM**. Docs, wiki pages, and tickets go through Graphify's
LLM doc extractor (`docify`, ADR-0023), which needs a chat model — a **Groq key is cheap and fast**.
Add to a repo-root `.env` (already gitignored — never commit it):

```sh
LLM_PROVIDER=groq
LLM_API_KEY=gsk_...
LLM_MODEL=llama-3.1-8b-instant
```

Then:

```sh
./scripts/bootstrap.sh --with-docs
```

This runs a *second*, incremental build over code + this repo's own docs (still `--backend local`
— no GitHub/ADO token needed, only the LLM key). `bootstrap.sh` never reads or prints the key's
value, only whether one is present. If this pass doesn't activate (bad key, rate limit, model
typo), the zero-LLM KB from the default run stays active and fully queryable — a failed *optional*
build never regresses what's already being served (invariant 5: a version activates only after its
own validation passes; the previous active version is untouched otherwise).

To index **your own** real GitHub/Azure DevOps sources instead of this repo, see
[00-getting-started.md](00-getting-started.md) Part 9.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Postgres isn't reachable at localhost:5432` | Start it: `brew services start postgresql@16` (macOS) or your Docker Postgres container. Using a non-default port/host? Pass `PGHOST=... PGPORT=...` before the script. |
| `createdb failed` / role errors | Homebrew Postgres names its superuser role after your macOS user, not `postgres`. Pass `PGUSER=<your-role>` (default is `$USER`, matching Homebrew); CI/Docker Postgres use `PGUSER=postgres`. |
| `uv sync` fails partway | Re-run `./scripts/bootstrap.sh` — `uv sync` is idempotent. A clean retry: `rm -rf services/<project>/.venv && ./scripts/bootstrap.sh`. |
| Build log shows a handful of `event=build_source_failed` lines but the script still reports `active` | Expected and fine — a single source's failure no longer aborts the whole build (each source commits independently); the `extractor_error_rate` publish gate only fails the *version* above a 1% threshold. |
| `alembic upgrade head` complains about a **downgraded/missing** table when you *also* run `make test-kb-builder` / `make verify` against the **same** database bootstrap used | kb-builder's own test suite runs migrations up **and down to base** on teardown against `TEST_DATABASE_URL` — it's designed to own a disposable test database, not the one `bootstrap.sh` built for you to browse. Keep them separate (bootstrap's `agentic_kb` for browsing/serving; `make migrate-test-db` + `TEST_DATABASE_URL=...postgres/agentic_kb_test` for `make verify`/`make test-*`) and the self-heal is automatic: `make test-mcp-server` / `make test-evals` both depend on `make migrate-test-db`, which re-migrates the shared test DB to head before the suite runs (see the Makefile's own comment on that dependency). |
| MCP server `/health` → `503 no_active_kb_version` | The KB isn't active. Re-run `./scripts/bootstrap.sh` and check for `build status : active` in step 4/5 — if it says `validation_failed`, read the named gate in the log tail it prints. |
| `address already in use` on `:8765` | An old server is still running: `pkill -f agentic_mcp_server`, or start this one on a different port (`MCP_HTTP_PORT=...`) and update the client config's URL to match. |

---

Deeper references once you're past onboarding: [01-design-deep-dive.md](01-design-deep-dive.md)
(architecture), [04-kb-builder-testing.md](04-kb-builder-testing.md) (switch LLM providers, the
full SQL query reference), [05-running-the-mcp-server.md](05-running-the-mcp-server.md) (server
config reference, Docker/compose).
