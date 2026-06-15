# 06 — End-to-end local walkthrough (understand the whole platform in one run)

This guide runs the **entire platform on your laptop** with one command and then explains, stage by
stage, what just happened and why. It needs only **Postgres + uv** — no Ollama, no Azure, no Entra.

## 0. Get the code (prerequisites)

Install **Postgres** (running locally) and **uv** (`brew install uv` or
`curl -LsSf https://astral.sh/uv/install.sh | sh`), then clone the repo and use the **`main`**
branch — that's the only branch you need; everything here ships on `main`:

```sh
# first time:
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform

# already cloned — get the latest main:
git checkout main
git pull origin main

# install per-service Python deps (uv manages Python 3.12 itself):
make sync
```

> `make demo` connects to Postgres as your `$USER` on `localhost:5432`. If your Postgres uses a
> different user/host/port, set `PGUSER` / `PGHOST` / `PGPORT` (e.g. `PGUSER=postgres make demo`).

## Run it

```sh
make demo          # or: ./scripts/e2e-local.sh
```

To *use* the KB afterwards, run the server alone against the demo database:

```sh
SKIP_BUILD=1 ./scripts/e2e-local.sh
```

## The shape of the system (read this first)

```
   ┌─────────────── BUILD PLANE (services/kb-builder) ───────────────┐
   sources ──connectors──▶ normalize ──▶ wikify/graphify ──▶ linker ──▶ embed ──▶ index
                                                   │                           │
                                                   ▼                           ▼
                                        ┌─────────────────────┐      ┌───────────────────┐
                                        │  POSTGRES (truth)   │◀────▶│  search index     │
                                        │  artifacts · edges  │      │  (projection)     │
                                        │  caches · ledger    │      └───────────────────┘
                                        └─────────────────────┘
                                                   ▲
   ┌─────────────── RUNTIME PLANE (services/mcp-server) ─────────────┐│
   agent ──MCP tools──▶ Context Broker ──(policy/budget/ACL/dedupe)──┘│
        create_pack · open_evidence · get_neighbors · verify_answer · ledger
```

Two ideas hold the whole thing together:

1. **Postgres is the single source of truth** (invariant 1). The search index is a *derived,
   rebuildable projection* — locally a JSON file, in production Azure AI Search; in the demo the
   server doesn't even use it, it keyword-searches Postgres directly.
2. **The agent never touches a data store.** Everything goes through the **Context Broker**, which
   enforces identity, ACLs, budgets, dedupe, and evidence-by-handle (invariants 3 & 6). The "agents"
   are markdown manifests (`agents/`) run by an LLM client (Claude Code / Copilot / OpenCode) — the
   broker is what makes them safe.

The demo walks the data left-to-right (build) then right-to-left (an agent asking questions).

---

## Stage 1 — A database (Postgres is the source of truth)

The script creates `agentic_kb_demo` and runs the **Alembic migrations** that `services/kb-builder`
owns. This is the canonical schema: `source_item`, `knowledge_artifact`, `knowledge_edge`,
`generation_cache`, `embedding_cache`, `kb_build_run`, `retrieval_event`. Everything else in the
platform is derived from these tables and can be thrown away and rebuilt.

> Why kb-builder owns migrations and mcp-server never runs them: the build plane evolves the schema;
> the runtime plane only reads it. They're separate `uv` projects that never import each other — the
> only thing they share is the markdown contracts in `docs/contracts/` (ADR-0008).

## Stage 2 — Build a knowledge base (incremental, versioned, gated)

The build CLI runs over a `sources.yaml`. The demo uses `scripts/demo-sources.yaml`, whose single
source matches **no files on purpose**, so the LLM-backed *wikify* pipeline never runs and the demo
is **zero-LLM**. All the content comes from the **`git_metadata` connector** (appended automatically):
one deterministic `commit` artifact per repo commit. What happens to each source:

1. **Connector → normalize.** A connector fetches a source and produces normalized content with a
   `content_hash`. Same input ⇒ same hash, always (determinism).
2. **Incremental gate (invariant 4).** If the `content_hash` matches what's already stored, the build
   **skips** chunk/wikify/graphify/embed/index entirely — no LLM call, no re-embed. Every model call
   is gated by a cache key. (Run `make demo` twice: the second build skips everything.)
3. **Generate.** Changed code → *graphify* (AST extraction, deterministic). Changed docs → *wikify*
   (the LLM step — skipped in this demo). `git_metadata` commits are pure data: no LLM at all.
4. **Linker + cross-domain.** Deterministic links, then a candidate generator and (optionally) an LLM
   judge produce graph edges with a **trust class** (EXTRACTED vs INFERRED_* — routing hints that can
   never support a cited claim).
5. **Embed + index.** A local hash embedder (deterministic, no cloud) vectorizes artifacts; the index
   projection is reconciled to the registry in both directions (orphans removed, missing members
   back-filled — ADR-0017).
6. **Publish gates + activation (invariant 5).** Index-consistency, citation integrity, extractor
   error rate, symbol-count delta, evidence recall. **A `kb_version` goes active only if every gate
   passes.** The CLI prints `build status : active` and the new `kb_version`.

Versioning is **interval membership** (ADR-0013): each artifact/edge carries `valid_from_seq` and
`invalidated_at_seq`, and "what the active version serves" is a `build_seq` range — not a label match.
This is how a rebuild that changes one file supersedes only the affected artifacts while everything
else is carried forward.

Inspect the result:

```sh
psql -d agentic_kb_demo -c \
 "SELECT artifact_type, count(*) FROM knowledge_artifact GROUP BY 1 ORDER BY 1;"
```

## Stage 3 — Serve it through the MCP Context Broker

The script starts the server on `127.0.0.1:8765` with **opt-in local-dev auth** (ADR-0016):

- `MCP_LOCAL_DEV_AUTH=1` swaps the Entra verifier for a fixed local identity — but only on a
  **loopback** bind and only with a **placeholder tenant**; it refuses to run next to a real tenant or
  a public bind, logs loudly, and marks the ledger. Production (flag unset) is byte-for-byte
  fail-closed Entra. There is no auth-off switch (invariant 6).
- The server retrieves candidates with **`PostgresKeywordSearchClient`** — the default `SearchClient`
  implementation that keyword-searches Postgres. So locally you need **no Azure Search**: the broker
  reads the same truth the build wrote.
- `GET /health` is an unauthenticated readiness probe — `200` once a version is active, `503` before.
- The script gives the demo subject a comfortable per-agent budget via
  `MCP_AGENT_ALLOWANCES='{"local-dev": {"max_requests": 20, "max_tokens": 50000}}'`. Budgets are
  real and **server-enforced** (invariant 3) — drop that value and `open_evidence` starts returning
  "allowance exceeded", which is the budget system doing its job, not a failure.

## Stage 4 — Be an agent: drive the five tools

`scripts/smoke_client.py` connects an MCP client (any bearer works under dev-auth) and calls the tools
in the order a real agent would. Each one demonstrates an invariant:

| Tool | What it does | What it proves |
|---|---|---|
| **`context.create_pack`** | Retrieves **once**, dedupes, reranks to ≤5 **evidence cards by handle** (L0/L1 — *not* raw text), enforces the run budget, writes a `retrieval_event` | Retrieve-once + budget + evidence-by-handle (invariant 3); identity/ACL/budget come from the authenticated session, never the request body |
| **`context.open_evidence`** | Expands **one** card to its raw L2 text by handle, metered against the pack budget, with a deterministic **injection scan** that *flags* (never rewrites) suspicious content | Raw text only by handle; retrieved text is **untrusted** and can't change tool policy (invariant 6) |
| **`graph.get_neighbors`** | Walks the Postgres `knowledge_edge` graph (EXTRACTED edges by default) | Graph behavior is exposed **only** through MCP tools (invariant 2). *(0 neighbors here — the zero-LLM demo builds commits without the code artifacts their edges point at; a full build shows edges.)* |
| **`context.verify_answer`** | Every claim must cite evidence ids; L0 runs mandatory, deterministic provenance checks (exists · in active version · ACL-visible · in your retrieval ledger · not stale · trust) and returns a signed-able **receipt** | The trust boundary: an answer is platform-trusted *iff* it carries a valid receipt (invariant 7). A claim citing evidence you never retrieved **fails** |
| **`ledger.list_retrievals`** | Returns the `retrieval_event` rows your run wrote (subject-scoped) | Every retrieval path is **ledgered**; you can audit exactly what the broker did on your behalf |

A real agent (the `orchestrator.md` manifest in `agents/`, rendered for Copilot/OpenCode) does the
same calls — pointing at `http://127.0.0.1:8765/mcp/` with a dev-auth bearer — only with an LLM
deciding the queries and writing the cited answer.

---

## Going further locally

- **A richer KB with real summaries/concepts** (exercises the LLM wikify path) — install Ollama and
  follow **dev-guide 04**; point the server at that database instead of the demo one.
- **Automated agent-task + retrieval-quality measurement** — `make eval-run` drives the broker through
  the eval harness and enforces the golden evidence-recall gate. This is the closest thing to an
  automated end-to-end agent test.
- **Browse the graph** — export an Obsidian vault (dev-guide 04 §Obsidian) and open it.
- **Query the registry** — the SQL reference is in dev-guide 04 §Querying the database.

## What runs locally vs. what's cloud-substituted

| Concern | Local (this demo) | Production |
|---|---|---|
| LLM (wikify / L3 entailment) | Ollama / OpenAI-compatible, or skipped | Azure OpenAI behind `ModelClient` |
| Embeddings | local hash embedder | Azure OpenAI |
| Search relevance | `PostgresKeywordSearchClient` | Azure AI Search behind `SearchClient` |
| Identity | opt-in dev-auth (loopback) | Entra ID, fail-closed |
| Fetch sources | local filesystem / git history | GitHub + Azure DevOps (PAT/managed identity) |
| Build trigger | you run the CLI | scheduled nightly CI *(not yet wired — see ADR-0004)* |

Everything that differs sits **behind an interface** (`SearchClient` / `ModelClient` / the auth
verifier), so the build, broker, budget, ACL, evidence, verifier, and ledger logic you exercise
locally is the *same code* that runs in production — only the swapped seam changes.
