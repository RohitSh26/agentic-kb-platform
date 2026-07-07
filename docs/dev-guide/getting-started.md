# Getting started

From nothing to a built, served, and answering knowledge base in about 10 minutes. One command
builds it, one command serves it, and a five-line script asks it your first question. No cloud
accounts, no API keys, no tokens.

## 1. Check the prerequisites

You need three tools. Python 3.12 is not one of them — `uv` downloads it automatically the first
time it is needed.

| Tool | Check | Install (macOS) |
|---|---|---|
| git | `git --version` | ships with Xcode CLT, or `brew install git` |
| uv | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` — then open a **new** terminal |
| Postgres 16 | `pg_isready` | `brew install postgresql@16 && brew services start postgresql@16` |

On Linux: `sudo apt-get install -y postgresql-16` and the same `uv` installer. No local
Postgres at all: `docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16`.

**You should see:**

```
$ git --version
git version 2.50.1 (Apple Git-155)
$ uv --version
uv 0.8.20 (3e6fd0b77 2025-09-22)
$ pg_isready
/tmp:5432 - accepting connections
```

## 2. Clone and bootstrap

```sh
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform
./scripts/bootstrap.sh
```

This takes 2–3 minutes: it installs dependencies, creates and migrates the `agentic_kb`
database, builds the knowledge base from the code in your checkout (zero LLM calls — no API key
needed), and verifies retrieval against it. It is safe to re-run at any time; it skips whatever
already exists.

**You should see** (trimmed to the significant lines):

```
== 1/5  Preflight checks
  git       — OK (git version 2.50.1 (Apple Git-155))
  uv        — OK (uv 0.8.20 (3e6fd0b77 2025-09-22))
  python3.12 — OK (already available to uv)
  psql      — OK (psql (PostgreSQL) 16.14 (Homebrew))
  postgres  — OK (reachable at localhost:5432)

== 2/5  Install dependencies (uv sync — kb-builder, mcp-server, review-panel, evals)
  -- services/kb-builder
Resolved 74 packages in 1ms
...

== 3/5  Create + migrate the database [agentic_kb]
  created database 'agentic_kb'
ts=... msg=Running upgrade  -> 0001, Create the canonical Knowledge Registry tables (architecture §6).
...
ts=... msg=Running upgrade 0022 -> 0023, v_retrieval_health mined-vs-unresolved split (PR-43, ADR-0034).
  schema is at head

== 4/5  Build the knowledge base (code + commits + aliases — zero LLM, ~1 minute)
  build status : active
  kb_version   : local.20260707T213832Z
  active version: local.20260707T213832Z
  search index : .kb-local-search-index.json

== 5/5  Smoke-verify: an active kb_version + a real, zero-LLM retrieval check
  active kb_version: local.20260707T213832Z
  running the alias-resolution retrieval check (real kb_search-path code, zero LLM)...
HIT   alias-01-alias-reference-index                query='the alias reference index'                             top1=docs/pr-briefs/PR-38-alias-reference-index.md
...
25/25 top-1 hits = 100.0%
PASS (target >= 80%)

== Done — a knowledge base is built, active, and verified in 'agentic_kb'
```

`build status : active` is the line that matters: the build passed its publish gates and this
version is now what the server serves. The final check resolves 25 real natural-language queries
against the knowledge base you just built.

## 3. Start the server

The MCP Context Broker serves your knowledge base to agents through budgeted, audited tools.
Start it and **leave it running**; do everything else in a second terminal.

```sh
cd services/mcp-server
MCP_LOCAL_DEV_AUTH=1 MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765 \
MCP_ENTRA_TENANT_ID=local-dev MCP_ENTRA_AUDIENCE=api://local MCP_LOCAL_DEV_TEAMS=platform \
MCP_AGENT_ALLOWANCES='{"local-dev": {"max_requests": 50, "max_tokens": 50000}}' \
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run python -m agentic_mcp_server
```

`MCP_LOCAL_DEV_AUTH=1` enables a loopback-only developer identity — it is not an "auth off"
switch; every request still passes the same permission and budget checks, and the server refuses
to start this way on a non-loopback address. `MCP_AGENT_ALLOWANCES` gives that identity a
comfortable `kb_search` budget: 50 calls and 50,000 tokens per session. Every variable is
documented in [the environment-variable reference](reference/environment-variables.md).

**You should see:**

```
ts=... level=INFO logger=agentic_mcp_server.mcp.server event=agent_allowances_loaded subjects=1
ts=... level=INFO logger=agentic_mcp_server.mcp.server event=client_registry_loaded clients=0
ts=... level=INFO logger=agentic_mcp_server.mcp.server event=trace_sink_selected kind=PostgresTraceSink
ts=... level=WARNING logger=agentic_mcp_server.auth.local_dev_selection event=local_dev_auth_enabled msg='LOCAL DEV AUTH ACTIVE — bearer verification is bypassed; never use in production' subject=local-dev teams=platform client_id=local-dev host=127.0.0.1
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)
```

The `local_dev_auth_enabled` warning is printed loudly on every start, by design — developer
auth can never be silently on.

## 4. Verify it is serving

In your second terminal:

```sh
curl -s http://127.0.0.1:8765/health
```

**You should see:**

```
{"status":"ok","service":"agentic-kb-context-broker","active_kb_version":"local.20260707T213832Z"}
```

`"status":"ok"` means the server is up and serving the version you just built. If you get
`no_active_kb_version` or `registry_unreachable` instead, see
[troubleshooting](how-to/troubleshoot.md).

## 5. Ask your first question

Still in the second terminal, from the repo root:

```sh
uv run --project services/mcp-server python - <<'EOF'
import asyncio
from fastmcp import Client

async def main() -> None:
    async with Client("http://127.0.0.1:8765/mcp/", auth="local-dev-token") as client:
        response = await client.call_tool("kb_search", {"request": {
            "query": "where is the per-agent token budget enforced?",
        }})
        for hit in response.data.results:
            print(f"{hit.title}  [{hit.artifact_type}]")
            print(f"   {hit.source_uri}")
        b = response.data.budget_remaining
        print(f"budget remaining: {b.calls} calls, {b.tokens} tokens")

asyncio.run(main())
EOF
```

**You should see:**

```
services/mcp-server/src/agentic_mcp_server/domain/token_budget.py  [code_file]
   file:///Users/edhaa/Development/agentic-kb-platform/services/mcp-server/src/agentic_mcp_server/domain/token_budget.py
Token estimation. Budgets themselves are enforced by the Context Broker (context  [code_symbol]
   file:///Users/edhaa/Development/agentic-kb-platform/services/mcp-server/src/agentic_mcp_server/domain/token_budget.py
Per-agent allowances. Budgets are enforced here, server-side — never by prompts.  [code_symbol]
   file:///Users/edhaa/Development/agentic-kb-platform/services/mcp-server/src/agentic_mcp_server/context_broker/budgets.py
services/mcp-server/src/agentic_mcp_server/context_broker/budgets.py  [code_file]
   file:///Users/edhaa/Development/agentic-kb-platform/services/mcp-server/src/agentic_mcp_server/context_broker/budgets.py
services/mcp-server/src/agentic_mcp_server/mcp/tool_registry.py  [code_file]
   file:///Users/edhaa/Development/agentic-kb-platform/services/mcp-server/src/agentic_mcp_server/mcp/tool_registry.py
budget remaining: 49 calls, 49450 tokens
```

(The `file://` paths point into your own checkout.) That answer came from your knowledge base:
permission-filtered, budget-metered, and written to the audit ledger.

## Where to go next

- [Tutorial 1 — Explore what got built](tutorials/01-explore-what-got-built.md): see the
  artifacts, aliases, and build health behind that answer.
- [Connect VS Code](how-to/connect-vscode.md): put `kb_search` in your editor's chat.
- Something did not behave as shown here? [Troubleshoot](how-to/troubleshoot.md).
