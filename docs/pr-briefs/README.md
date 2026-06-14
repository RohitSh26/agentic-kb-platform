# PR Briefs — the build queue

Implement these in order, one PR per change, using the `implement-pr` skill (`/next-pr`). Each brief
is scoped to a single reviewable PR. Do not exceed a brief's scope; record anything extra as an open
question. Architecture references point at `docs/architecture/00-overview.md` and `docs/adr/`.

| PR | Scope |
|----|-------|
| PR-01 | Project scaffold and contracts |
| PR-02 | Postgres schema and migrations |
| PR-03 | Connector skeletons |
| PR-04 | Incremental build engine |
| PR-05 | Wikify pipeline |
| PR-06 | Graphify adapter |
| PR-07 | Linker |
| PR-08 | Azure AI Search indexer |
| PR-09 | MCP server base |
| PR-10 | Context Broker |
| PR-11 | Agent markdown manifests |
| PR-12 | Evaluation harness |
| PR-13 | Security hardening |
| PR-14 | YAML source configuration |
| PR-15 | Portable agent framework (.copilot / .opencode) |
| PR-16 | Native subagent + skill declarations in the portable renderings |
| PR-17 | Docker compose for the whole system (two service containers + Postgres) |
| PR-18 | Open the `read_pack` role field to team-defined agents |
| PR-19 | Deployment-time per-agent budget allowances |
| PR-20 | Adopter-side parity checker + pinned-minimum parity tests |
| | **Unified graphify + trust contract** (ADR-0010, ADR-0011) — phases 1→4 |
| PR-21 | Phase 1 · Deterministic Python AST code extractor (FileGraph producer) |
| PR-22 | Phase 1 · Local-filesystem fetch backend + `build` CLI (end-to-end into Postgres) |
| PR-23 | Phase 1 · Trust class on edges + trust-aware traversal (`trust_floor`) |
| PR-24 | Phase 1 · L0 provenance verifier + minimal verification receipt |
| PR-25 | Phase 1 · Golden-query evals (evidence-recall) + publish gates |
| PR-26 | Phase 2 · Deterministic cross-domain links (git metadata + work-item refs) |
| PR-27 | Phase 2 · Identity-over-time invalidation + enforcing relation gates |
| PR-28 | Phase 3A · Cross-domain candidate generator + audit table (no promotion) |
| PR-29 | Phase 3B · LLM relationship judge over bounded candidates → INFERRED edges |
| PR-30 | Phase 4 · Claim/evidence ledger + verifier L1 (coverage) + L2 (typed-fact) |
| PR-31 | Phase 4 · Verifier L3 (LLM entailment, cached) + signed receipts |
| PR-32 | Phase 4 · Client/app identity + scopes + official-client enforcement |
| PR-33 | Phase 4 · Temporal semantics (current code vs stale docs vs historical cards) |
