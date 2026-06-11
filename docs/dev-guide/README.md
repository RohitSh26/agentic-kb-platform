# Developer Guide

Onboarding documentation for engineers joining the Agentic KB Platform.

- [01 — Platform design deep dive](01-design-deep-dive.md): what we are building, the two planes,
  the Postgres Knowledge Registry, and the architecture invariants with pointers to where each is
  enforced in code.
- [02 — Implementation tour](02-implementation-tour.md): a guided walk through both planes as
  implemented (PR-01 → PR-16): contracts, schema, connectors, incremental build engine, wikify,
  graphify, linker, indexing, the MCP server and Context Broker, security hardening, source
  configuration, and the portable agent framework.
- [03 — Local testing](03-local-testing.md): how to run everything on a laptop with a local
  Postgres and in-memory fakes — no Azure resources required.

Deep specs live in `docs/architecture/`, decisions in `docs/adr/`, build units in `docs/pr-briefs/`.
