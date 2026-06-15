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

Deep specs live in `docs/architecture/`, decisions in `docs/adr/` (through ADR-0015), build units
in `docs/pr-briefs/` (through PR-33), cross-service agreements in `docs/contracts/`.
