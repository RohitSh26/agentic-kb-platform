# 01 — Run the platform

This page takes you from a fresh clone to a **built, served, and verified knowledge base** on your
own machine, and shows you how to keep it fresh as code changes. It is the one entry point: if you
only read one page, read this one. No cloud accounts, no API keys, no tokens on the default path.

## What you're about to run

Three pieces, in order:

```
   ┌─ 1. KB BUILDER ─┐      ┌─ 2. MCP SERVER ─┐      ┌─ 3. YOUR EDITOR/AGENT ─┐
   reads code/docs         serves the KB to            you ask questions;
   builds a knowledge  ──▶  agents through one    ──▶  the agent calls the server,
   graph in Postgres       budgeted, audited door      answers grounded in the KB
```

1. **The KB Builder** reads source code (and optionally docs/wiki/tickets) and writes a knowledge
   graph — functions, files, commits, and how they connect — into a **Postgres** database.
2. **The MCP server** (the *Context Broker*) serves that knowledge to AI agents through a small
   set of tools, enforcing budgets, permissions, and an audit log. Agents never hold database
   credentials — KB access goes through this one door.
3. **Your editor or agent host** (VS Code Copilot, Copilot CLI, OpenCode, …) asks questions; the
   agent calls the broker's `kb_search` tool and answers from the results, naming its sources.

This page covers pieces 1 and 2. Piece 3 is
[02 — Connect your editor](02-connect-your-editor.md).

## Prerequisites

| Tool | Why | Check | Install (macOS/Homebrew) |
|---|---|---|---|
| **git** | clone the repo | `git --version` | ships with Xcode CLT, or `brew install git` |
| **uv** | manages Python 3.12 + all four projects' dependencies | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (open a **new** terminal after) |
| **Postgres 16** | the Knowledge Registry — the one real piece of infrastructure | `pg_isready` | `brew install postgresql@16 && brew services start postgresql@16` |

That's it. Python 3.12 itself is **not** a separate prerequisite — `uv` fetches it automatically
the first time it's needed. Not on macOS/Homebrew? Linux install commands are in
[22 — Testing and builds](22-testing-and-builds.md) §1, or use Docker for Postgres:
`docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16`.

**No API key is needed for the default build.** For doc summaries and every other key this
platform can use, see [07 — Providers and API keys](07-providers-and-api-keys.md).

## Bootstrap: the one command

```sh
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform
./scripts/bootstrap.sh
```

Takes about 2–3 minutes on a machine with the three prerequisites (most of that is `uv sync`
downloading packages the first time). **Safe to re-run**: it skips whatever already exists and the
build itself is incremental.

Override the database name (default `agentic_kb`) for a throwaway proof run:

```sh
./scripts/bootstrap.sh --db-name agentic_kb_proof     # or: DB_NAME=... ./scripts/bootstrap.sh
```

## What success looks like, stage by stage

**1/5 Preflight** — a line per tool, ending `postgres — OK (reachable at localhost:5432)`. Any
failure names the exact thing missing and the command to fix it; nothing later runs until this
passes.

**2/5 `uv sync`** — one block per project (`services/kb-builder`, `services/mcp-server`,
`services/review-panel`, `evals`), each ending with `uv`'s own "Resolved / Installed" summary and
no errors.

**3/5 Database** — either `database 'agentic_kb' already exists — skipping createdb` (a re-run) or
`created database 'agentic_kb'`, then a scroll of Alembic `Running upgrade ... -> ...` lines
ending in `schema is at head`.

**4/5 The build** — a scroll of structured `event=...` build-log lines (source upserts, cache
lookups, graphify/linker/alias-miner activity), ending in the four lines the build CLI always
prints:

```
build status : active
kb_version   : local.20260707T091500Z
active version: local.20260707T091500Z
search index : .kb-local-search-index.json
```

`build status : active` is the one line that matters — the publish gates
(`docs/contracts/publish-gates.md`) passed and this version is now what the MCP server serves.
This default build is **code + commits + aliases only** — zero LLM calls, confirmed by
`llm_calls=0` in the `event=build_run_completed` log line.

**5/5 Smoke-verify** — two checks, both real:

```
active kb_version: local.20260707T091500Z
running the alias-resolution retrieval check (real kb_search-path code, zero LLM)...
HIT   alias-01-alias-reference-index    query='the alias reference index'   top1=docs/pr-briefs/PR-38-alias-reference-index.md
...
25/25 top-1 hits = 100.0%
PASS (target >= 80%)
```

The first check queries for an active version — a bootstrap with no active KB is a failure, not a
warning. The second (`scripts/eval_alias_resolution.py`) resolves 25 real natural-language queries
against the live `alias_reference` table your build just wrote — the same table the server's
`kb_search` / `get_task_context` path reads — and asserts ≥80% top-1 accuracy. It proves the build
produced *retrievable, resolvable* knowledge, not just rows in a table.

Then a **Next steps** block prints — the same commands as the sections below.

**Confirm the graph directly, if you like:**

```sh
psql agentic_kb -c "select artifact_type, count(*) from knowledge_artifact where invalidated_at_seq is null group by 1 order by 2 desc;"
psql agentic_kb -c "select count(*) as edges from knowledge_edge where invalidated_at_seq is null;"
```

You should see a few hundred artifacts (code symbols, files, commits, alias entries) and a few
hundred edges. The full SQL health reference is
[05 — Database operations](05-database-operations.md).

## Serve it: start the MCP server

The KB is only useful once something serves it. Start the broker in a terminal and **leave it
running**; do everything else in a second one:

```sh
cd services/mcp-server
MCP_LOCAL_DEV_AUTH=1 MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765 \
MCP_ENTRA_TENANT_ID=local-dev MCP_ENTRA_AUDIENCE=api://local MCP_LOCAL_DEV_TEAMS=platform \
MCP_AGENT_ALLOWANCES='{"local-dev": {"max_requests": 50, "max_tokens": 50000}}' \
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run python -m agentic_mcp_server
```

What the settings mean, in plain language:

- `MCP_LOCAL_DEV_AUTH=1` — use a simple **local developer identity** instead of corporate login.
  It only works on `127.0.0.1` and refuses to run on a public address. It is **not** an "auth off"
  switch — every request still flows through the same permission and budget checks; production
  auth is fail-closed Entra ID with no override (ADR-0016).
- `MCP_AGENT_ALLOWANCES` — gives your local identity a comfortable `kb_search` budget (50 calls /
  50,000 tokens per session) so your first questions aren't cut off. Budgets are real and enforced
  by the server — see [03 — Using the knowledge tools](03-using-the-knowledge-tools.md).
- `DATABASE_URL` — points the server at the knowledge base you just built.

**Verify it's up** — in the second terminal:

```sh
curl -s http://127.0.0.1:8765/health
# -> {"status":"ok", ..., "active_kb_version":"local...."}
```

`"status":"ok"` means the server is serving your KB. `/health` needs no token — it is a readiness
probe: **503 `no_active_kb_version`** means the server is up but the registry has no active KB
(build one); **503 `registry_unreachable`** means it can't reach Postgres (check `DATABASE_URL`).
A clean boot also logs `event=agent_allowances_loaded subjects=N` and
`event=client_registry_loaded clients=N`.

**(Optional) prove the full tool chain without an editor:**

```sh
# from the repo root, second terminal
MCP_URL=http://127.0.0.1:8765/mcp/ \
  uv run --project services/mcp-server python scripts/smoke_client.py
```

This drives the governed broker path end to end and prints what each step proves, ending in
`smoke passed`. (That path is the citation-grade flow described in
[03 — Using the knowledge tools](03-using-the-knowledge-tools.md), not the everyday `kb_search`.)

### Server configuration reference

The server reads everything from the environment (`services/mcp-server/.../config.py`). Three
variables are **required** — it refuses to start without them (fail-fast, no silent defaults):

| Variable | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | **yes** | asyncpg URL of the Postgres holding the active KB. Must start `postgresql+asyncpg://`. May point at a remote Postgres — e.g. over an SSH tunnel (`ssh -L 5432:localhost:5432 build-host`); the server only ever *serves* what that registry holds. |
| `MCP_ENTRA_TENANT_ID` | **yes** | Entra tenant id (an identifier, not a secret) the bearer's issuer must match. `local-dev` in local-dev mode. |
| `MCP_ENTRA_AUDIENCE` | **yes** | the `aud` your access token must carry, e.g. `api://agentic-kb`. |
| `MCP_AGENT_ALLOWANCES` | no | subject → per-agent budget allowance JSON (identifiers only); empty ⇒ defaults. |
| `MCP_CLIENT_REGISTRY` | no | client_id → scopes + verification policy JSON (identifiers only). |
| `MCP_HTTP_HOST` | no | transport bind host; default `0.0.0.0`. |
| `MCP_HTTP_PORT` | no | transport port; default `8000`. |
| `MCP_HTTP_PATH` | no | streamable-HTTP mount path; default `/mcp/`. |

`MCP_HTTP_*` affect the transport only — never the broker's auth, budgets, ACLs, or ledger. No
secrets live in config: token verification is JWKS-based (the server holds no client secret).

Local-dev identity variables (all optional beyond the flag itself):

| Var | Default | Meaning |
|---|---|---|
| `MCP_LOCAL_DEV_AUTH` | unset → **OFF** | truthy (`1/true/yes/on`) enables the loopback-only dev verifier |
| `MCP_LOCAL_DEV_SUBJECT` | `local-dev` | the dev identity's subject (what the ledger records) |
| `MCP_LOCAL_DEV_TEAMS` | `local-dev-team` | csv of teams granted to the dev identity (your ACLs) |
| `MCP_LOCAL_DEV_CLIENT_ID` | = subject | optional dev `client_id` for the platform-trust tool |

With local-dev auth on, any `Authorization: Bearer <anything>` authorizes as the dev subject — the
request still flows through the normal ACL / scope / trust checks, and the server logs
`event=local_dev_auth_enabled` loudly on every start so dev auth can never be silently on.
**Guardrails**: a real tenant id or a non-loopback bind refuses to boot. Never set it in a
deployed image. For **real Entra tokens** (any non-loopback deployment), see
[07 — Providers and API keys](07-providers-and-api-keys.md) §"Broker bearer tokens".

Two things the server never does, by design: it never **builds** and never **runs migrations** —
kb-builder owns the schema; the server only serves the last successfully activated `kb_version`.

### Docker instead

The image's `CMD` runs the identical entrypoint as the direct `uv run` command above, so there is
no behavioral drift between the two. The bare image needs only the three required variables:

```sh
docker build -t agentic-mcp-server ./services/mcp-server
docker run --rm -p 8000:8000 \
  -e DATABASE_URL="postgresql+asyncpg://postgres:postgres@host.docker.internal:5432/agentic_kb" \
  -e MCP_ENTRA_TENANT_ID="<your-tenant-guid>" \
  -e MCP_ENTRA_AUDIENCE="api://agentic-kb" \
  agentic-mcp-server
```

The root `docker-compose.yml` runs the whole stack — Postgres, the one-shot migration job, and the
server (`docker compose up --build`; add `--profile build` to also build + activate a KB so it
comes up serving). Details, invariants, and the compose Postgres URL:
[22 — Testing and builds](22-testing-and-builds.md) §"Docker".

## Rebuilding when code changes

The build is **incremental** — re-running it after code changes only reprocesses what actually
changed. Re-run the same command any time:

```sh
./scripts/bootstrap.sh          # skips deps/DB that already exist, rebuilds incrementally
```

On a rebuild with few or no changes, expect `event=build_skip_unchanged` for most sources and
near-zero `llm_calls` (cache hits). The newly activated version still serves the **complete**
knowledge set, not just the day's delta — version membership is interval-based, so unchanged
artifacts carry forward automatically.

The local search index is a persistent JSON file (`.kb-local-search-index.json`) — a derived,
rebuildable projection of Postgres, never truth. It carries forward across incremental rebuilds;
you never need to manage it in normal use.

## The one-time fresh rebuild (provenance check)

There is exactly one situation an incremental rebuild cannot fix: a database first built **before
the builder started stamping source provenance**. Such databases carry `source_item` rows whose
identity columns (`repo`, `branch`, `external_id`) are `NULL`. The builder heals these columns
whenever a source's *content* changes, but a source that never changes again keeps its stale row
forever. Check yours:

```sh
psql agentic_kb -c "select count(*) from source_item where repo is null and source_type in ('github_code','github_doc');"
```

- `0` — nothing to do (any recently bootstrapped database is fine).
- Non-zero — do a **one-time fresh rebuild**:

```sh
pkill -f agentic_mcp_server          # stop the server so dropdb isn't blocked
dropdb --force agentic_kb
./scripts/bootstrap.sh
```

Leave the `.kb-local-search-index.json` file in place — the first rebuild's orphan sweep removes
the stale entries and re-projects fresh ones (it self-heals). To force a fully clean projection
instead, delete the file before rebuilding.

## Optional: add doc/wiki/ticket summaries

The default build is code-only and zero-LLM. Prose sources (docs, wiki pages, tickets) are
summarized by a chat model, so this pass needs one key — a Groq key is cheap and fast. Add to a
repo-root `.env` (gitignored — never commit it):

```sh
LLM_PROVIDER=groq
LLM_API_KEY=gsk_...
LLM_MODEL=llama-3.1-8b-instant
```

Then:

```sh
./scripts/bootstrap.sh --with-docs
```

This runs a second, incremental build over code + this repo's own docs (no GitHub/ADO token
needed, only the LLM key). `bootstrap.sh` never reads or prints the key's value, only whether one
is present. If this pass doesn't activate (bad key, rate limit, model typo), the zero-LLM KB from
the default run **stays active and fully queryable** — a failed optional build never regresses
what's already being served. Other providers (OpenAI, Azure OpenAI, Claude, Ollama):
[07 — Providers and API keys](07-providers-and-api-keys.md).

## Optional: build from YOUR real GitHub + Azure DevOps sources

The default build indexes this repo's own code. To index **your** repositories and Azure DevOps
Wiki / Work Items, use the production fetch backend. It needs access tokens and — for prose
sources — an LLM key.

**1. Put credentials in the repo-root `.env`** (never commit it):

```sh
GITHUB_TOKEN=ghp_...           # GitHub PAT: classic with `repo` scope, OR fine-grained granted to the repo (Contents: Read)
ADO_PAT=...                    # Azure DevOps PAT: Wiki (Read) + Work Items (Read) — only if you index ADO
LLM_PROVIDER=groq              # only needed if your sources include prose
LLM_API_KEY=gsk_...
LLM_MODEL=llama-3.1-8b-instant
```

Code is zero-LLM — a code-only build needs no `LLM_*` at all.

**2. Describe your sources** — copy the template and edit the identifiers (you set `owner/repo`,
`organization`, `project` — not URLs):

```sh
cp services/kb-builder/sources.example.yaml scripts/my-sources.yaml
# edit: your GitHub owner/repo, your ADO org/project/wiki; delete source types you don't want
```

**3. Build into a fresh database with the production backend:**

```sh
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"
dropdb --if-exists --force agentic_kb && createdb agentic_kb
( cd services/kb-builder && DATABASE_URL="$DATABASE_URL" uv run alembic upgrade head )

cd services/kb-builder
set -a; source ../../.env; set +a          # load GITHUB_TOKEN, ADO_PAT, LLM_*
DATABASE_URL="$DATABASE_URL" \
  uv run python -m agentic_kb_builder.build \
    --sources ../../scripts/my-sources.yaml --workspace ../.. \
    --backend production --no-git-metadata --log-format timeline
cd ../..
```

(`--no-git-metadata` is deliberate: you're indexing a *remote* repo, so this checkout's local
commits don't belong in that KB.) Then serve and connect exactly as above. Fetch errors (404/401/
403): [08 — Troubleshooting](08-troubleshooting.md) §"Real-source fetches". The full build-CLI
flag table and provider runbook: [22 — Testing and builds](22-testing-and-builds.md).

> The GitHub backend is exercised against the live API; the ADO backends are unit-tested against
> mocked transports, so a real ADO instance may surface format/auth specifics to iron out.

## Run the evals

```sh
make eval-all
```

Runs every evaluation tier that *can* run in your environment; tiers needing something you haven't
set up (`TEST_DATABASE_URL`, `DATABASE_URL`, LLM creds) **skip with a stated reason** rather than
failing or inventing a number. What each tier checks:
`docs/architecture/evaluation-system.md`.

## Stop and clean up

```sh
pkill -f agentic_mcp_server     # stop the server (or Ctrl-C in its terminal)
dropdb agentic_kb               # (optional) delete the knowledge base entirely
```

## What runs locally vs in production

Everything that differs sits behind an interface, so the build, broker, budgets, permissions,
verifier, and audit logic you run locally is the *same code* that runs in production — only the
swapped seam changes:

| Concern | Local (this page) | Production |
|---|---|---|
| AI model (summaries) | none for code; Groq/OpenAI/Ollama for prose | Azure OpenAI |
| Embeddings | local hash embedder | Azure OpenAI |
| Search | keyword-search over Postgres | Azure AI Search |
| Identity | local-dev (loopback only) | Entra ID, fail-closed |
| Sources | your local checkout / GitHub + ADO via PAT | GitHub + ADO via managed identity |

---

**Next:** connect a host — [02 — Connect your editor](02-connect-your-editor.md) — then learn what
the tools actually give you: [03 — Using the knowledge tools](03-using-the-knowledge-tools.md).
Anything not behaving as described here: [08 — Troubleshooting](08-troubleshooting.md).
