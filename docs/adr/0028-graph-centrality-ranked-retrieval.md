# ADR-0028 — Graph-centrality ranked retrieval: rank by the graph we already store

## Status

Accepted and Implemented (2026-06-22). Builds on the Postgres knowledge graph (ADR-0002,
`knowledge_edge`) and the existing broker ranker (`context_broker/retrieval.py`, PR-33 temporal
weighting). First increment shipped by PR-36: migration `0019` (`knowledge_artifact.centrality_score`)
and the deterministic build-time PageRank step folded into the broker's `_rank_key`. Query-time
personalized PageRank (Increment 2) remains a follow-up.

## Context

We store a real code + knowledge graph in Postgres (`knowledge_edge`: `defined_in`, `imports`,
`calls`, `documents`, judged `INFERRED_*`, …) but we barely *rank* with it. Today retrieval ranks by
`_rank_key = (source_backed, authority_score, keyword_score × temporal_weight, id)` and graph traversal
(`context.expand`) is **breadth-first** — every neighbour is treated equally; structural importance is
ignored. The graph is used for *reachability*, not *relevance*.

Independent research converges hard on the opposite: rank by graph centrality.

- **Aider's repo map** ranks every symbol by **PageRank over the dependency graph** and fills a
  ~1k-token budget in rank order — "no other agent uses graph-theoretic relevance ranking," and it is
  now widely copied.
- **GraphRAG** (Microsoft) and **HippoRAG / HippoRAG2** retrieve by **personalized PageRank** over a
  knowledge graph rather than by vector similarity; **MixPR** does sparse PageRank retrieval for
  long-context. PageRank/centrality is the SOTA relevance signal for graph-shaped corpora.
- The 2026 consensus for coding agents is **agentic search as the backbone, the index only where it
  adds signal a plain grep can't** (Anthropic dropping RAG; Cursor/Sourcegraph/etc.). A grep agent
  *cannot* compute "which symbol is structurally central across the whole repo / across repos." That
  is exactly the signal our graph can provide — our natural differentiator.

We are uniquely positioned: the graph already exists in Postgres, versioned by `build_seq`. We should
turn it from storage into a ranking advantage.

## Decision

Adopt **graph centrality as a first-class ranking signal**, in two increments.

### Increment 1 (PR-36) — build-time global centrality prior

1. **Compute PageRank over `knowledge_edge`** in the build plane, as a new step in `_finalize_graph`
   that runs **after the linker, judge, AND the invalidation pass** (so it ranks the *served*,
   post-sweep live set — ghost edges and deleted-source artifacts already retired), and **before**
   index reconciliation + activation, within the same pre-activation transaction. The edge read uses
   the **ADR-0013 interval-membership predicate** — `valid_from_seq <= S AND (invalidated_at_seq IS
   NULL OR invalidated_at_seq > S)` for the active build_seq `S` — **not** `valid_from_seq = S`: an
   incremental build rewrites only changed sources, so the live graph includes still-valid edges from
   prior builds; ranking over only this build's new edges would zero almost every artifact's centrality
   (a silent regression). Deterministic power-iteration (fixed damping, iterations, tolerance, sorted
   node order, deterministic dangling-node mass redistribution) — pure, no new heavy dependency, no
   LLM, cache-irrelevant (it is graph math; recomputed every build that touches edges, by design).
2. **Store a normalized `centrality_score ∈ [0,1]` on `knowledge_artifact`** (nullable Float, same
   shape/precedent as `authority_score` / `freshness_score`). Pinned in the shared contract
   `docs/contracts/postgres-knowledge-registry.md`.
3. **Fold it into the broker ranker as a prior, not an override.** In `retrieval.py:_rank_key`, the
   structural prior lifts the keyword relevance term:
   `keyword_score × temporal_weight × (1 + β·centrality)` (β small, e.g. 0.25), with the
   `source_backed` and `authority` tiers and the `artifact_id` tie-break **unchanged**. Exact-match
   relevance and provenance still dominate; centrality breaks ties and lifts structurally-central
   nodes among comparable matches. A `centrality = 0` (or NULL) artifact ranks exactly as today, so the
   change is backward-safe and a neutral graph is a no-op.

### Increment 2 (follow-up) — query-time personalized PageRank

Seed PPR from the query-hit nodes and random-walk-with-restart over the graph to rank the *connected*
neighbourhood (the HippoRAG pattern), replacing the BFS in `context.expand` with centrality-ordered
expansion. Deferred: it adds request-time compute and needs its own perf/eval pass; the global prior
delivers most of the value first and is free at request time.

## Consequences

- Retrieval ranks by structural importance, not just keyword overlap — the graph becomes a relevance
  engine, the thing a plain grep agent cannot replicate (especially cross-repo).
- One nullable column + one deterministic build step + a one-line ranker change; fully backward-safe
  (NULL/0 ⇒ today's ordering). The prior is **transparent and logged**, never a hidden reranker (same
  discipline as the PR-33 temporal weight).
- Centrality is build-scoped and recomputed each build from the active edges; it is derived data, not
  truth, and never a citation.
- Adds a shared-contract column, so both services' registry-contract tests must be updated in lockstep.

## Alternatives considered and rejected

- **Query-time PPR first.** Rejected as the *first* step: heavier (request-time graph walk), needs a
  perf budget and eval; the global prior is free at request time and captures most of the lift. PPR is
  increment 2.
- **A separate centrality table / external graph DB.** Rejected: violates the "graph in Postgres, no
  graph DB in V1" invariant; a column on the artifact is sufficient and rebuildable.
- **Vector/semantic rerank instead.** Rejected as the primary signal: the whole project thesis (and the
  industry) is that for code, structural/agentic signals beat embedding similarity; semantic stays an
  *augmentation* (ADR-0019), not the ranker.
- **Let the model do it (no ranking).** Rejected: "which node is central across the repo" is a global
  graph computation the model cannot do from local reads — it is precisely the value we add.

## Follow-ups

- PR-36: migration (centrality column), deterministic build-time PageRank step, broker `_rank_key`
  fold-in, contract + tests both sides.
- Increment 2: query-time personalized PageRank in `context.expand` (seeded by query hits), with a perf
  budget and an evals pass (retrieval precision / evidence-recall lift vs. BFS).
- Evals: measure ranking quality (golden-query evidence-recall) with vs. without the centrality prior;
  tune β from logs.
