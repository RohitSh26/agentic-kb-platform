# Developer Guide

Onboarding documentation for engineers joining the Agentic KB Platform.

- [01 — Platform design deep dive](01-design-deep-dive.md): what we are building, the two planes,
  the Postgres Knowledge Registry, and the architecture invariants with pointers to where each is
  enforced in code.
- [02 — Implementation tour](02-implementation-tour.md): a guided walk through the build plane as
  implemented (PR-01 → PR-07): contracts, schema, connectors, incremental build engine, wikify,
  graphify, linker.
- [03 — Local testing](03-local-testing.md): how to run everything on a laptop with a local
  Postgres and in-memory fakes — no Azure resources required.

Deep specs live in `docs/architecture/`, decisions in `docs/adr/`, build units in `docs/pr-briefs/`.
