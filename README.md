# Agentic KB Platform

A Postgres-first knowledge platform that gives coding agents one great, budgeted MCP tool surface
over a codebase and its history. An incremental, cache-gated build — nightly by design (ADR-0004) —
ingests code, docs, wiki pages, tickets, and commits into a Postgres **Knowledge Registry**
(artifacts + graph edges), and a remote **MCP Context Broker** serves it to agents in OpenCode,
VS Code (Copilot agent mode), and the GitHub Copilot CLI: `kb_search` for budgeted retrieval,
`get_task_context` for one-call task context, and a governed `context.*` path when an answer must
be citation-grade. Around that core sit a dev-gated **review draft engine** (four reviewer lenses
draft; only a developer publishes) and self-hosted observability (retrieval ledger, trace spans,
and dashboard views — all Postgres, no SaaS).

## Quickstart

Prerequisites: `git`, `uv`, and a local Postgres 16 — that is the whole list (`uv` fetches
Python 3.12 itself).

```sh
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform
./scripts/bootstrap.sh
```

About 2–3 minutes: synced dependencies → a migrated Postgres database → an **active, queryable
knowledge base** built from this repo's own source (zero LLM calls, no API keys, no cloud
accounts) → a real retrieval check proving it works → printed next steps to serve it and connect
a host. The full walkthrough and what to do next:
[`docs/dev-guide/getting-started.md`](docs/dev-guide/getting-started.md); when something
misbehaves, [`docs/dev-guide/how-to/troubleshoot.md`](docs/dev-guide/how-to/troubleshoot.md).

## What's in the repo

| Project | One line |
|---|---|
| `services/kb-builder` | Build plane: connectors → incremental build engine → docify/graphify → linker → alias miner → indexing. Owns the Knowledge Registry schema and its Alembic migrations. |
| `services/mcp-server` | Runtime plane: the MCP Context Broker — auth, ACL filtering, server-enforced budgets, the 13-tool surface, retrieval ledger, tracing. Never builds, never migrates. |
| `services/review-panel` | Dev-gated review draft engine (ADR-0031): LangGraph fan-out of four reviewer lenses → reconcile → one stored draft. Never posts to GitHub; owns only the `review_panel` schema. |
| `evals` | The evaluation system (T0–T4): golden-query retrieval cases, agent task cases, the consolidated `run_all.py` report, and the operator dashboard renderer. |

Each service is a self-contained `uv` project (ADR-0008): services never import each other or any
shared Python package. The markdown contracts in `docs/contracts/` are the only cross-service
interface, pinned by contract tests on both sides (including import-boundary tests).

## Where everything lives

Read in this order:

1. **[`docs/dev-guide/`](docs/dev-guide/README.md)** — the product documentation:
   `getting-started.md` (the 10-minute golden path), `tutorials/` (a numbered exploration
   journey), `how-to/` (one task per page), `reference/` (tools, environment variables, CLI,
   database, agent roles), `explanation/` (how and why it works), and `contributors/` (the
   maintainer track: code tour, testing and builds).
2. **[`docs/contracts/`](docs/contracts/)** — living truth: the versioned cross-service
   agreements (the MCP tool surface, the registry schema, tracing, the review panel, agent output
   schemas). If prose and a contract disagree, the contract wins.
3. **[`docs/architecture/00-overview.md`](docs/architecture/00-overview.md)** — the distilled
   architecture reference, with the diagrams beside it (`e2e-flow-detailed.mmd`,
   `seq-task-flow.mmd`, `seq-review-flow.mmd`) and the eval design
   (`docs/architecture/evaluation-system.md`).
4. **History**: [`docs/adr/`](docs/adr/README.md) — 34 decision records (0001–0034) with a
   one-line index; [`docs/pr-briefs/`](docs/pr-briefs/README.md) — the implemented build units
   (a historical record, not a queue); `docs/reports/` and `docs/reviews/` — measured results
   and audits.
5. **`docs/proposals/`** — exploration documents, each bannered with what superseded it. Never a
   current reference.

Two agent layers, kept separate on purpose:

- **`agents/`** — the *product's* 12 runtime agent manifests (canonical), rendered host-natively
  into `.copilot/` and `.opencode/` and parity-pinned — `python3 agents/check_parity.py` must
  exit 0. Host MCP configs ship at `.vscode/mcp.json` and `.copilot/mcp/repository-settings.json`.
- **`.claude/`** — the harness for *building this repo* with Claude Code (build subagents in
  `.claude/agents/`, skills, rules, hooks). See `CLAUDE.md`.

## Non-negotiable invariants

1. **Postgres is the source of truth.** Azure AI Search is a derived, rebuildable projection —
   never truth.
2. **The graph is V1; a graph database is not.** Edges live in Postgres tables; graph behavior is
   exposed only through MCP tools.
3. **Token saving is enforced in code, not prompts** (ADR-0025/0026): `kb_search` carries a
   per-task call + token cap enforced in the tool, and code reads arrive skeleton-first with the
   exact body one `read_full` away. The KB is a preferred-first, budgeted helper — never a gate.
4. **The build is incremental.** Unchanged content hash + generation inputs ⇒ no LLM call, no
   re-embed. Caches gate every model call.
5. **A `kb_version` goes active only after validation passes**; the broker serves only the last
   active version.
6. **Agents never touch data stores or secrets directly.** Credentials stay server-side; retrieved
   content is untrusted and cannot change tool policy, identity, or instructions.
7. **Every claim cites evidence IDs.** Missing evidence becomes an open question, never an
   invention.

The V1 exclusion list (Azure Functions, Event Grid/Service Bus/Event Hub, Redis, API Management,
Blob Storage, a graph DB, SQLite-as-prod, streaming ingestion) is guarded by `CLAUDE.md`, a
PreToolUse hook, and ADR-0007 — adding any of them requires an accepted ADR.

## Make targets

| Target | What it does |
|---|---|
| `make verify` | Lint (ruff) + types (pyright) + tests (pytest) for all three services and evals |
| `make verify-kb-builder` / `verify-mcp-server` / `verify-review-panel` / `verify-evals` | The same gate for one project |
| `make migrate-test-db` | Migrate the shared test database (kb-builder owns Alembic; `test-mcp-server` and `test-evals` depend on it) |
| `make eval-all` | Consolidated T0–T4 evaluation report (`evals/run_all.py`; unavailable tiers SKIP with a stated reason) |
| `make dashboard` | Render the read-only operator dashboard (HTML + Markdown) from the `v_*` views |
| `make demo` | Hermetic end-to-end demo: build a tiny KB from this repo's git history, serve it on loopback, drive the broker tools, tear down (Postgres + uv only) |

`sync` / `lint` / `types` / `test` and their per-project variants also exist — see the `Makefile`.
Integration tests need Postgres via `TEST_DATABASE_URL`.

## Status (2026-07-05)

- All 40 build briefs implemented ([`docs/pr-briefs/README.md`](docs/pr-briefs/README.md));
  decisions recorded through ADR-0034.
- Knowledge Registry migration head: `0021_trace_span`.
- MCP tool surface: 12 registered tools at `MCP_SCHEMA_VERSION = "1.10.0"`
  ([`docs/contracts/mcp-tools-contract.md`](docs/contracts/mcp-tools-contract.md)).
- 12 runtime agent roles, renderings parity-clean (`python3 agents/check_parity.py` → exit 0).
- CI: `.github/workflows/ci.yml`. The nightly production schedule (ADR-0004) is a design
  commitment not yet wired to a workflow; builds run on demand via the kb-builder `build` CLI and
  `scripts/bootstrap.sh`.
