# Developer Guide

Onboarding documentation for engineers joining the Agentic KB Platform.

- [01 — Platform design deep dive](01-design-deep-dive.md): what we are building, the two planes,
  the Postgres Knowledge Registry, the trust contract (trust buckets, the layered verifier, signed
  receipts), and the architecture invariants with pointers to where each is enforced in code.
- [02 — Implementation tour](02-implementation-tour.md): a guided walk through both planes as
  implemented (PR-01 → PR-33): contracts, schema, connectors (local-FS + the production GitHub/ADO
  backends), incremental build engine, wikify, graphify, the linker (deterministic, cross-domain,
  candidate→judge), version-membership invalidation, indexing, the MCP server and Context Broker,
  the verifier ladder + signed receipts, client identity + scopes, temporal ranking, security
  hardening, source configuration, the portable agent framework, and the deployment follow-ons.
- [03 — Local testing](03-local-testing.md): how to run everything on a laptop with a local
  Postgres and in-memory fakes — no Azure resources required, including the end-to-end `build` CLI
  and the Obsidian vault export.
- [04 — KB-builder testing (from a bare machine)](04-kb-builder-testing.md): a complete copy-paste
  runbook for a brand-new machine — install the toolchain, point at **any** LLM provider (Ollama /
  Groq / OpenAI / Azure OpenAI / Azure Foundry / Claude / any OpenAI-compatible endpoint), run a
  local and a production build, export to Obsidian, and a full **database query reference** for
  checks/analysis (build health, the served set, ghost-edge + cache/cost checks).
- [05 — Running the MCP server (fresh separate machine)](05-running-the-mcp-server.md): start the
  **Context Broker** against an already-built KB — without Docker (`uv run python -m
  agentic_mcp_server`) and with Docker/compose, the `DATABASE_URL` / `MCP_*` env reference, the
  fail-closed Entra auth setup (and the proposed local-dev alternative, ADR-0016), the `/health`
  probe (200 vs 503), and a worked `create_pack → open_evidence → graph.get_neighbors →
  verify_answer` walk through the tools to **use** the KB.
- [06 — End-to-end local walkthrough](06-end-to-end-walkthrough.md): **start here to understand the
  whole system.** One command (`make demo`) builds a KB, serves it through the broker, and drives all
  five tools — Postgres + uv only, no Ollama/Azure — with a stage-by-stage explanation of how the
  build plane, the registry, the Context Broker, and the agent tools fit together and which invariant
  each step enforces.
- [07 — What "MCP ready" means](07-what-mcp-ready-means.md): a plain-language explainer (with a worked
  example prompt) of the agent's context toolkit — create_pack, context.expand, open_evidence,
  verify_answer — and why an agent gets the full connected context cheaply and cited instead of
  reading whole files.
- [08 — Run the whole system from scratch](08-run-everything-from-scratch.md): **fresh-Mac, copy-paste
  reproduction** of the entire chain — build a KB (zero token, zero LLM), serve the broker, drive the
  gated multi-agent runner with a Groq model, and replay the run. Needs only Postgres + uv + a Groq key.

Deep specs live in `docs/architecture/`, decisions in `docs/adr/` (through ADR-0017), build units
in `docs/pr-briefs/` (through PR-33), cross-service agreements in `docs/contracts/`.
