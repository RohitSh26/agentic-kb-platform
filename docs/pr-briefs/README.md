# PR Briefs — historical record of build units

This is a **historical record**, not a build queue: all 40 briefs below have been implemented and
merged. Each brief was scoped to a single reviewable PR; nothing here is pending work. For the
current architecture read `docs/architecture/00-overview.md`; for why a design looks the way it
does, read `docs/adr/` (index: `docs/adr/README.md`). Per-brief status stamps are unnecessary —
this index carries status for all of them.

| PR | Scope | Status |
|----|-------|--------|
| PR-01 | Project scaffold and contracts | Implemented |
| PR-02 | Postgres schema and migrations | Implemented |
| PR-03 | Connector skeletons | Implemented |
| PR-04 | Incremental build engine | Implemented |
| PR-05 | Wikify pipeline | Implemented |
| PR-06 | Graphify adapter | Implemented |
| PR-07 | Linker | Implemented |
| PR-08 | Azure AI Search indexer | Implemented |
| PR-09 | MCP server base | Implemented |
| PR-10 | Context Broker | Implemented |
| PR-11 | Agent markdown manifests | Implemented |
| PR-12 | Evaluation harness | Implemented |
| PR-13 | Security hardening | Implemented |
| PR-14 | YAML source configuration | Implemented |
| PR-15 | Portable agent framework (.copilot / .opencode) | Implemented |
| PR-16 | Native subagent + skill declarations in the portable renderings | Implemented |
| PR-17 | Docker compose for the whole system (two service containers + Postgres) | Implemented |
| PR-18 | Open the `read_pack` role field to team-defined agents | Implemented |
| PR-19 | Deployment-time per-agent budget allowances | Implemented |
| PR-20 | Adopter-side parity checker + pinned-minimum parity tests | Implemented |
| | **Unified graphify + trust contract** (ADR-0010, ADR-0011) — phases 1→4 | |
| PR-21 | Phase 1 · Deterministic Python AST code extractor (FileGraph producer) | Implemented |
| PR-22 | Phase 1 · Local-filesystem fetch backend + `build` CLI (end-to-end into Postgres) | Implemented |
| PR-23 | Phase 1 · Trust class on edges + trust-aware traversal (`trust_floor`) | Implemented |
| PR-24 | Phase 1 · L0 provenance verifier + minimal verification receipt | Implemented |
| PR-25 | Phase 1 · Golden-query evals (evidence-recall) + publish gates | Implemented |
| PR-26 | Phase 2 · Deterministic cross-domain links (git metadata + work-item refs) | Implemented |
| PR-27 | Phase 2 · Identity-over-time invalidation + enforcing relation gates | Implemented |
| PR-28 | Phase 3A · Cross-domain candidate generator + audit table (no promotion) | Implemented |
| PR-29 | Phase 3B · LLM relationship judge over bounded candidates → INFERRED edges | Implemented |
| PR-30 | Phase 4 · Claim/evidence ledger + verifier L1 (coverage) + L2 (typed-fact) | Implemented |
| PR-31 | Phase 4 · Verifier L3 (LLM entailment, cached) + signed receipts | Implemented |
| PR-32 | Phase 4 · Client/app identity + scopes + official-client enforcement | Implemented |
| PR-33 | Phase 4 · Temporal semantics (current code vs stale docs vs historical cards) | Implemented |
| | **ADR-0025/0027/0028 rethink + ADR-0030 twelve-role rebuild** | |
| PR-34 | Deterministic code `search_text` enrichment (ADR-0018 Phase 2) — closes the concept/identifier keyword-recall gap, still zero LLM for code | Implemented |
| PR-35 | Crash-durable model-output cache (ADR-0027) — a crashed build never re-pays for already-spent docify/embedding tokens | Implemented |
| PR-36 | Graph-centrality ranking prior (ADR-0028, increment 1) — build-time PageRank over `knowledge_edge` folded into the broker's rank key | Implemented |
| PR-37 | Ship the real `kb_search` MCP tool (ADR-0025, ADR-0030) — the budgeted retrieval tool every one of the twelve agent roles depends on | Implemented |
| PR-38 | Alias/reference index: deterministic build-time mining (ADR-0030) — resolves terse phrases to code entities, zero LLM | Implemented |
| PR-39 | `get_task_context`: one-call task context on LangGraph (ADR-0030) — scope + blast radius + conventions + similar changes, zero LLM at query time | Implemented |
| PR-40 | Review-panel draft engine: LangGraph fan-out/join, dev-gated publication (ADR-0030, amended by ADR-0031) — four specialist lenses fan out, reconcile, persist a draft; never posts | Implemented |
