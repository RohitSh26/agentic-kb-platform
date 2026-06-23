# PR-36 — Graph-centrality ranking prior (ADR-0028, increment 1)

## Why

We store a code+knowledge graph in Postgres but rank retrieval almost entirely by keyword overlap;
`context.expand` is plain BFS. SOTA (Aider repo-map PageRank, GraphRAG, HippoRAG) ranks by graph
centrality — the one relevance signal a plain grep agent cannot compute. Compute a deterministic
PageRank over `knowledge_edge` at build time, store a normalized `centrality_score` on the artifact,
and fold it into the broker ranker as a transparent prior (not an override). Backward-safe: a
NULL/0 centrality ranks exactly as today.

## Scope

- **Migration (kb-builder owns the schema):** add `knowledge_artifact.centrality_score` (nullable
  `Float`), same shape as `authority_score`/`freshness_score`. Forward + rollback; backfill none
  (recomputed on the next build).
- **Deterministic PageRank (kb-builder):** `application/centrality.py` — a pure power-iteration
  PageRank over the live `knowledge_edge` rows, read with the **ADR-0013 interval-membership predicate**
  `valid_from_seq <= S AND (invalidated_at_seq IS NULL OR invalidated_at_seq > S)` for the active
  build_seq `S` (NOT `valid_from_seq = S` — an incremental build's live graph includes still-valid
  prior-build edges; ranking over only new edges would zero most scores). Fixed damping (0.85),
  max-iterations + tolerance, **sorted node order**, and **deterministic dangling-node mass
  redistribution** (uniform to all nodes) so the result is bit-identical run to run. No new heavy
  dependency (hand-rolled ~40 LOC), no LLM. Normalize to `[0,1]` (divide by max; guard the all-zero
  graph against div-by-zero). Run it as a step in `build_runner._finalize_graph`, **AFTER** the linker,
  judge, AND `run_invalidation_pass` (so it ranks the served post-sweep set), BEFORE index
  reconciliation + activation, within the single pre-activation transaction; write `centrality_score`
  per member artifact.
- **Broker ranker (mcp-server):** add `centrality_score` to the `ArtifactRow` DTO + the
  `fetch_artifacts` SELECT; fold it into `context_broker/retrieval.py:_rank_key` as
  `base_score × temporal_weight × (1 + BETA·centrality)` (BETA=0.25, a named module constant). Leave
  the `source_backed` and `authority` tiers and the `artifact_id` tie-break unchanged. Keep it
  transparent/logged like the temporal weight.
- **Contract:** update `docs/contracts/postgres-knowledge-registry.md` with the new column; update both
  services' registry-contract tests in lockstep (mcp-server reads the column via raw SQL, never an ORM).
- **Tests:**
  - kb-builder (pure PageRank): a more-referenced node scores higher than a leaf; **bit-identical**
    scores across two runs AND across a shuffled input-edge order (determinism guard); a graph with
    **some dangling nodes** (no out-edges) converges and stays deterministic; empty/edgeless ⇒ all-zero
    (no div-by-zero); normalization ∈ [0,1]; idempotent on rebuild.
  - kb-builder (membership): a still-valid **prior-build** edge participates in centrality on an
    incremental build (the interval-predicate guard — proves we don't rank over only new edges).
  - mcp-server: `_rank_key` lifts a high-centrality artifact above an equal-keyword-score low-centrality
    one; a NULL/0 centrality reproduces today's ordering (backward-safe); `source_backed`/`authority`
    tiers still dominate centrality; the centrality factor appears in the retrieval log line.

## Do NOT

- Do not change `context.expand` BFS in this PR (query-time personalized PageRank is ADR-0028
  increment 2, a separate brief).
- Do not let centrality override the `source_backed`/`authority` tiers or become a hidden reranker —
  it is a transparent multiplicative prior on the relevance term only.
- Do not add a graph DB or a separate centrality table (invariant: graph in Postgres); no new heavy
  dependency for PageRank.
- Do not treat centrality as truth/citation — it is build-scoped derived data.

## Acceptance criteria

- [ ] Migration adds `centrality_score` (nullable Float) with forward + rollback.
- [ ] Deterministic PageRank step runs in `_finalize_graph` **after `run_invalidation_pass`**, before
      activation, reading edges via the interval-membership predicate; writes normalized
      `centrality_score` per member artifact; bit-identical across runs + shuffled input; dangling nodes
      handled; prior-build still-valid edges participate (tests).
- [ ] `_rank_key` folds centrality as `×(1 + BETA·centrality)` (BETA a named constant); NULL/0 ⇒
      identical ordering to today (test); source_backed/authority tiers unchanged (test); the centrality
      factor is surfaced in the retrieval log line (transparent-factors rule).
- [ ] Shared contract + both registry-contract tests updated in lockstep.
- [ ] `make verify-kb-builder` + `make verify-mcp-server` green; ruff + pyright clean; no excluded-V1
      resource; local Ollama build still works.
