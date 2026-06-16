# 08 — Run the whole system from scratch (fresh Mac)

> Build a KB → serve it through the MCP Context Broker → drive the **gated multi-agent
> orchestration** with a Groq model → **replay** the whole run. Every command below was run
> end-to-end on macOS; the minimal path needs **no GitHub/ADO tokens, no Ollama, and no LLM
> for the build** — just Postgres, uv, and a Groq API key (for the agent brains).

The chain you are standing up:
`KB build → connected graph (defined_in/calls/imports) → search_text → MCP broker →
context.expand (role-scoped, shared) → trust + ACL + budget → audit ledger →
gated multi-agent runner → replay`.

---

## 0. Prerequisites

- **Postgres 16**, reachable locally, with a role you can `createdb` as. The commands use
  peer auth as `$USER` (Homebrew Postgres default): `brew install postgresql@16 && brew services start postgresql@16`.
- **uv** (manages Python 3.12 per service): `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- **A Groq API key** — only the *agent runner* uses an LLM (the broker is deterministic and
  the minimal build is zero-LLM). Get one at console.groq.com.
- *(Optional, not needed for this guide)* Ollama + a real GitHub/ADO PAT — only for the
  full-fidelity build in §7.

```sh
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform
make sync            # uv sync for both services + evals
```

Create a repo-root `.env` with the Groq settings the runner reads:

```sh
# .env  (repo root)
LLM_PROVIDER=groq
LLM_API_KEY=gsk_your_groq_key_here
LLM_MODEL=llama-3.1-8b-instant
# LLM_BASE_URL is optional — the runner defaults it from LLM_PROVIDER.
```

---

## 1. Create the registry database + migrate it

kb-builder owns the schema; migrate it to head once.

```sh
createdb agentic_kb_local
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb_local"
( cd services/kb-builder && DATABASE_URL="$DATABASE_URL" uv run alembic upgrade head )
```

---

## 2. Build the KB from the local code (zero token, zero LLM)

Builds this repo's own Python source from your local checkout (`--backend local`). Code
extraction is deterministic — **no LLM, no Ollama**. The `GITHUB_TOKEN` is a dummy: the
config loader requires the env var to be *present*, but `--backend local` reads local files
and ignores its value.

```sh
cd services/kb-builder
GITHUB_TOKEN="local-unused" DATABASE_URL="$DATABASE_URL" \
  uv run python -m agentic_kb_builder.build \
    --backend local --workspace ../.. \
    --sources ../../scripts/local-code-sources.yaml \
    --no-git-metadata --index-path /tmp/kb_local_index.json
cd ../..
```

Expect: `llm_calls=0`, `publish_gates_passed`, `kb_version_activated`, `build status : active`.
Sanity check (≈1,500 code symbols, a connected graph, search_text populated):

```sh
psql agentic_kb_local -c "select artifact_type, count(*) from knowledge_artifact where invalidated_at_seq is null group by 1 order by 2 desc;"
psql agentic_kb_local -c "select count(*) as edges from knowledge_edge where invalidated_at_seq is null;"
```

---

## 3. Serve it through the MCP Context Broker

Local-dev auth is loopback-only and opt-in (ADR-0016) — **not** an auth-off switch. Leave this
running in its own terminal.

```sh
cd services/mcp-server
MCP_LOCAL_DEV_AUTH=1 MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765 \
MCP_ENTRA_TENANT_ID=local-dev MCP_ENTRA_AUDIENCE=api://local MCP_LOCAL_DEV_TEAMS=platform \
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb_local" \
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
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb_local"
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
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb_local" \
  uv run --project services/mcp-server python -m agentic_mcp_server.replay <run_id>
```

You'll see `governance.checkpoint` (orchestrator→human, then orchestrator→each subagent),
`context.create_pack` (cards + scores + budget), and `context.expand` (seeds → connected
cards + tokens) — one timeline per request.

---

## 6. Tear down

```sh
# stop the broker: Ctrl-C in its terminal (or)  pkill -f agentic_mcp_server
dropdb agentic_kb_local      # optional: discard the local KB
```

---

## 7. (Optional) Full-fidelity build — real sources + the semantic layer

The minimal path above is **code-only** and skips the semantic (embeddings + LLM-judge) layer
— which a task-context eval showed adds ~0 to *task* context (the deterministic graph does the
work). To reproduce the full build instead:

- Real sources: use `scripts/test-sources.yaml` (or your own), set real `GITHUB_TOKEN` +
  `ADO_PAT`, and build with `--backend production`.
- Semantic edges: run Ollama (`ollama pull nomic-embed-text`) and add
  `EMBEDDINGS_PROVIDER=ollama RELATIONSHIP_JUDGE=1` to the build command (adds prose↔code
  edges via the LLM judge; one-time cost, cached). See dev-guide 06 for the build narrative.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `auth.token_env GITHUB_TOKEN is not set` on a local build | Set the dummy `GITHUB_TOKEN="local-unused"` (§2) — required present, value ignored by `--backend local`. |
| `/health` → 503 `no_active_kb_version` | The build didn't activate — re-run §2 and check for `kb_version_activated`. |
| runner: `LLM_API_KEY ... required` | Your `.env` lacks the Groq key, or you didn't `source .env` in the runner's shell (§4). |
| runner: `Connection refused` to `:8765` | The broker isn't running — start §3 first. |
| `must use the asyncpg driver` | `DATABASE_URL` must start `postgresql+asyncpg://`. |
| tool call → 401 | Local-dev auth not enabled, or a non-loopback host — see §3 (it refuses non-loopback). |
