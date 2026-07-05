# Developer Guide

Onboarding documentation for engineers joining the Agentic KB Platform. Read it as a path: the
three tiers below go from "make it run" to "understand it" to "operate it".

## Start here

1. **[00 — Quickstart](00-quickstart.md)** — in a hurry? The scripted 10-minute path: one command
   (`scripts/bootstrap.sh`) from a fresh clone to an **active, queryable knowledge base**, plus a
   real zero-LLM retrieval check proving it works. No tokens, no cloud accounts.
2. **[00 — Getting Started](00-getting-started.md)** — the narrated version, and the one document
   to follow on a brand-new machine: from `git clone` to GitHub Copilot in VS Code answering
   questions about this codebase through the budgeted, audited KB — install, build (no
   tokens/LLM), serve, connect, ask, audit, then real GitHub/ADO sources and the multi-agent
   runner. Troubleshooting throughout.
3. **[01 — Platform design deep dive](01-design-deep-dive.md)** — *what* we are building, *why* it
   is shaped this way, and *where* each design rule is enforced in code: the planes, the Postgres
   Knowledge Registry, the trust contract, and the architecture invariants.

## When you need it

- **[02 — Implementation tour](02-implementation-tour.md)** — a guided walk through the code as it
  exists today, organized by subsystem: contracts, schema (migration head 0021), connectors, the
  incremental build engine, docify/graphify/linker, alias mining, indexing, the 12-tool Context
  Broker surface, the verifier ladder and receipts, identity and ACLs, and the agent framework.
- **[03 — Local testing](03-local-testing.md)** — run everything on a laptop with **uv + a local
  Postgres** and in-memory fakes; no Azure resources, including the end-to-end `build` CLI and the
  Obsidian vault export.
- **[04 — KB-builder testing (from a bare machine)](04-kb-builder-testing.md)** — a complete
  copy-paste runbook: install the toolchain, point at any LLM provider (Groq / OpenAI / Azure /
  Ollama / Claude / any OpenAI-compatible endpoint), run local and production builds, export to
  Obsidian, and a full database query reference for build health and cost checks.
- **[05 — Running the MCP server (fresh separate machine)](05-running-the-mcp-server.md)** — start
  the **Context Broker** against an already-built KB, with and without Docker: the
  `DATABASE_URL` / `MCP_*` env reference, fail-closed Entra auth (and the ADR-0016 local-dev
  alternative), the `/health` probe, and a worked walk through the **governed path**
  (`create_pack → open_evidence → graph.get_neighbors → verify_answer`) for citation-grade answers.
- **[07 — What "MCP ready" means](07-what-mcp-ready-means.md)** — a plain-language explainer of
  the agent's context toolkit and the **KB-first/file-fallback** model (ADR-0025): the KB as a
  fast, budgeted librarian — not a gate — with code arriving skeleton-first (ADR-0026).
- **[09 — GitHub Copilot CLI against the broker](09-copilot-cli-end-to-end.md)** — drive a real
  external agent (the GitHub Copilot CLI, using its own model) against the broker through the
  repo's committed, policy-carrying MCP config, which exposes exactly the tools the twelve-role
  canon grants: the budgeted `kb_search` and the one-call `get_task_context`. Every call —
  including the ones the budget refused — lands in the ledger.
- **[10 — VS Code (Copilot agent mode) against the broker](10-vscode-against-the-broker.md)** —
  redirect stub: this flow is Parts 6–8 of [00 — Getting Started](00-getting-started.md); the
  connection config ships at `.vscode/mcp.json`.

## Operating

- **[06 — Review panel](06-review-panel.md)** — run the **review draft engine**
  (`services/review-panel`): a four-lens LangGraph review of a pull request that persists **one
  draft** the developer pulls, edits, and publishes — the panel itself never posts to GitHub
  (ADR-0031).
- **[08 — Observability](08-observability.md)** — the dashboard, traces, and the retrieval ledger:
  answer "what happened, and what did it cost?" read-only over Postgres — `make dashboard`, the
  `v_*` views, `trace_span` / `TRACE_SINK` (ADR-0032), and ledger queries.

---

Deep specs live in `docs/architecture/` (start with
[`00-overview.md`](../architecture/00-overview.md)), cross-service agreements in
`docs/contracts/`. Decisions run through ADR-0032 and build units PR-01–40, **all implemented** —
see [`docs/adr/README.md`](../adr/README.md) and [`docs/pr-briefs/README.md`](../pr-briefs/README.md).
