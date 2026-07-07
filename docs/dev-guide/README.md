# Developer Guide

How to run, use, and work on the Agentic KB Platform. The guide is split into two tracks: pages
**01–08** are for anyone who wants to *run and use* the platform (task-first, no history, no
architecture required); pages **20–22** are for people *changing* the platform. History —
decisions, build records, measured results — lives in `docs/adr/`, `docs/pr-briefs/`, and
`docs/reports/`, **not here**.

## Run and use the platform

Read **01 → 02 → 03** in order; reach for **04–08** as the need arises.

| Page | One line |
|---|---|
| **[01 — Run the platform](01-run-the-platform.md)** | From clone to a built, served, verified KB: prerequisites, `bootstrap.sh`, what success looks like, serving the MCP server, incremental rebuilds, and the one-time fresh-rebuild case. |
| **[02 — Connect your editor](02-connect-your-editor.md)** | VS Code Copilot (agent mode), GitHub Copilot CLI, and OpenCode — each connected in ≤5 steps with one real question walked through, plus the ledger proof it was governed. |
| **[03 — Using the knowledge tools](03-using-the-knowledge-tools.md)** | `kb_search` and `get_task_context` day to day, budget notices, KB-first/file-fallback, and (as an aside) the governed citation-grade path. |
| **[04 — Review drafts](04-review-drafts.md)** | Get a four-lens PR review draft, edit it, publish it under your own name — the panel never posts to GitHub. |
| **[05 — Database operations](05-database-operations.md)** | Postgres recipes: connect, health checks, backup/restore, reset, the useful-queries cookbook, maintenance, test-DB quirks. |
| **[06 — Observability](06-observability.md)** | "What happened, and what did it cost?" — `make dashboard`, the `v_*` views, per-step traces, and reading the retrieval ledger. |
| **[07 — Providers and API keys](07-providers-and-api-keys.md)** | The one reference for every key: which component needs one, per-provider setup (Groq/OpenAI/Azure/Claude/Ollama), where keys live, broker bearer tokens. |
| **[08 — Troubleshooting](08-troubleshooting.md)** | Every known failure mode by symptom: build gates, server health, ports, editors not seeing tools, budget notices, locks, database errors. |

## Work on the platform

Read **20–22**, then go deep in [`docs/architecture/`](../architecture/00-overview.md) and the
ADR index ([`docs/adr/README.md`](../adr/README.md)).

| Page | One line |
|---|---|
| **[20 — Architecture for contributors](20-architecture-for-contributors.md)** | What we're building and why it's shaped this way, ending in the invariants → enforcement map every change is reviewed against. |
| **[21 — Code tour](21-code-tour.md)** | A dated, subsystem-by-subsystem walk through the code — structure over specifics. |
| **[22 — Testing and builds](22-testing-and-builds.md)** | The verify gate, test databases and fakes, Docker compose, and the complete bare-machine build runbook (providers, flags, SQL health reference). |

---

Deep specs: [`docs/architecture/`](../architecture/00-overview.md). Cross-service agreements
(the source of truth when prose disagrees): `docs/contracts/`. History: `docs/adr/` (decision
records), `docs/pr-briefs/` (the implemented build units), `docs/reports/` (measured results).
