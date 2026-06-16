# 08 — Run the whole system from scratch (fresh Mac)

> Build a KB from your **real GitHub + Azure DevOps sources** → serve it through the MCP
> Context Broker → drive the **gated multi-agent orchestration** with a Groq model → **replay**
> the run. Every command below was run end-to-end on macOS against the real sources.

The chain you are standing up:
`KB build (GitHub code + docs, ADO wiki + work items) → connected graph
(defined_in/calls/imports) → search_text → MCP broker → context.expand (role-scoped, shared)
→ trust + ACL + budget → audit ledger → gated multi-agent runner → replay`.

---

## 0. Prerequisites

- **Postgres 16**, reachable locally. The commands use peer auth as `$USER` (Homebrew default):
  `brew install postgresql@16 && brew services start postgresql@16`. If your Postgres uses a
  password/role, adjust the `DATABASE_URL` accordingly.
- **uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- A repo-root **`.env`** with your real credentials (you said you'll provide these):

```sh
# .env  (repo root)
GITHUB_TOKEN=ghp_...            # PAT with read access to the repo you index
ADO_PAT=...                     # Azure DevOps PAT (Wiki + Work Items read) — if you index ADO
LLM_PROVIDER=groq
LLM_API_KEY=gsk_...             # Groq key — wikify (build) + the agent brains (runner)
LLM_MODEL=llama-3.1-8b-instant
```

- *(Optional)* **Ollama** — only for the semantic (embedding) graph layer in §2b:
  `ollama pull nomic-embed-text`. The deterministic graph + search_text do not need it.

```sh
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform
make sync            # uv sync for both services + evals
```

The source set lives in **`scripts/test-sources.yaml`** (GitHub code + docs, ADO wiki + work
items). Point it at *your* repo/org by editing `repo:` / `organization:` / `project:`; it reads
from the cloud at build time. (Or use `services/kb-builder/sources.example.yaml` as a template.)

---

## 1. Create the registry database + migrate it

kb-builder owns the schema; migrate it to head once.

```sh
createdb agentic_kb
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"
( cd services/kb-builder && DATABASE_URL="$DATABASE_URL" uv run alembic upgrade head )
```

---

## 2. Build the KB from your real sources (`--backend production`)

Fetches GitHub code + docs and ADO wiki + work items for real. Code is extracted
deterministically (zero-LLM); prose (docs/wiki/cards) is summarised by the Groq model from
your `.env`.

```sh
cd services/kb-builder
set -a; source ../../.env; set +a          # GITHUB_TOKEN, ADO_PAT, LLM_*
DATABASE_URL="$DATABASE_URL" \
  uv run python -m agentic_kb_builder.build \
    --sources ../../scripts/test-sources.yaml --workspace ../.. \
    --backend production --no-git-metadata --log-format timeline
cd ../..
```

Expect a real-time timeline ending in `publish_gates_passed`, `kb_version_activated`,
`build status : active`. Sanity check (code graph + search_text):

```sh
psql agentic_kb -c "select artifact_type, count(*) from knowledge_artifact where invalidated_at_seq is null group by 1 order by 2 desc;"
psql agentic_kb -c "select count(*) as edges from knowledge_edge where invalidated_at_seq is null;"
```

### 2b. (Optional) Add the semantic layer

The build above is deterministic graph + wikified prose. To also add the LLM-judged prose↔code
edges (needs Ollama for embeddings + Groq for the judge — a one-time, cached cost):

```sh
ollama pull nomic-embed-text     # once
EMBEDDINGS_PROVIDER=ollama RELATIONSHIP_JUDGE=1 DATABASE_URL="$DATABASE_URL" \
  uv run python -m agentic_kb_builder.build \
    --sources ../../scripts/test-sources.yaml --workspace ../.. \
    --backend production --no-git-metadata --log-format timeline
```

> A task-context eval showed the semantic layer adds ~0 to *task-context recall* — the
> deterministic graph does the work — so 2b is optional for testing the agent flow.

---

## 3. Serve it through the MCP Context Broker

Local-dev auth is loopback-only and opt-in (ADR-0016) — **not** an auth-off switch. Leave this
running in its own terminal.

```sh
cd services/mcp-server
MCP_LOCAL_DEV_AUTH=1 MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765 \
MCP_ENTRA_TENANT_ID=local-dev MCP_ENTRA_AUDIENCE=api://local MCP_LOCAL_DEV_TEAMS=platform \
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run python -m agentic_mcp_server
```

Verify (no token needed for health):

```sh
curl -s http://127.0.0.1:8765/health
# {"status":"ok", ... "active_kb_version":"local.<...>"}
```

Optional — the single-agent worked path (create_pack → open → graph → expand → verify → ledger):

```sh
MCP_URL=http://127.0.0.1:8765/mcp/ \
  uv run --project services/mcp-server python scripts/smoke_client.py
```

---

## 4. Run the gated multi-agent system (Groq brain)

In a second terminal. The orchestrator plans, then **pauses for your approval at every
delegation** (`[a]pprove / [e]dit / [r]eject / [x]abort`); build agents pull the deep code via
`context.expand` into the shared pack (once, reused); planners stay on the overview.

```sh
cd agentic-kb-platform
set -a; source .env; set +a
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"
export MCP_URL="http://127.0.0.1:8765/mcp/"

# Interactive — you approve each gate:
uv run --project services/mcp-server python scripts/agent_runner.py \
  "Add input validation to the GitHub connector"

# Or hands-free (auto-approve every gate), for a quick smoke:
uv run --project services/mcp-server python scripts/agent_runner.py --auto-approve \
  "Add input validation to the GitHub connector"
```

You'll see: a plan → gate → `create_pack` (5 cards) → a gate before each subagent →
`context.expand` at the first build role (e.g. 3 seeds → ~245 connected cards) →
`verify_answer: passed`. It prints the `run_id` and a replay command at the end.

> The generated code is **rough** — that's the small 8B Groq model, on purpose. This tests the
> *plumbing* (right-code retrieval, gates, trust, audit). A stronger model lifts quality with
> zero plumbing changes.

---

## 5. Replay — review exactly what happened

The "prove everything worked" view: every action and every approval gate, in time order.

```sh
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run --project services/mcp-server python -m agentic_mcp_server.replay <run_id>
```

You'll see `governance.checkpoint` (orchestrator→human, then orchestrator→each subagent),
`context.create_pack` (cards + scores + budget), and `context.expand` (seeds → connected
cards + tokens) — one timeline per request.

---

## 6. Tear down

```sh
# stop the broker: Ctrl-C in its terminal (or)  pkill -f agentic_mcp_server
dropdb agentic_kb            # optional: discard the KB
```

---

## 7. (Optional) No-tokens build — local files only

To exercise the chain with **no GitHub/ADO tokens and no LLM** (code-only, deterministic), build
this repo's own source from your local checkout instead of §2:

```sh
cd services/kb-builder
GITHUB_TOKEN="local-unused" DATABASE_URL="$DATABASE_URL" \
  uv run python -m agentic_kb_builder.build \
    --backend local --workspace ../.. \
    --sources ../../scripts/local-code-sources.yaml --no-git-metadata
```

`--backend local` reads local files; the dummy `GITHUB_TOKEN` is required *present* but its value
is ignored. You still need a Groq key for the §4 runner.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| build: `GITHUB_TOKEN`/`ADO_PAT` not set | `set -a; source ../../.env; set +a` before the build (§2). |
| build: GitHub/ADO **404** | The PAT is expired/lacks scope, or the repo/org/project in `test-sources.yaml` isn't yours — fix the token or the identifiers. |
| `/health` → 503 `no_active_kb_version` | The build didn't activate — re-check §2 for `kb_version_activated`. |
| runner: `LLM_API_KEY ... required` | `.env` lacks the Groq key, or you didn't `source .env` in the runner's shell (§4). |
| runner: `Connection refused` to `:8765` | The broker isn't running — start §3 first. |
| `must use the asyncpg driver` | `DATABASE_URL` must start `postgresql+asyncpg://`. |
| tool call → 401 | Local-dev auth not enabled, or a non-loopback host — see §3. |
