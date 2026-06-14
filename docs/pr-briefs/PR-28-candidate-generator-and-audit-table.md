# PR-28 — Cross-domain candidate generator + audit table (phase 3A, no promotion)

## Why

Candidate-then-judge is the architecture, not an optimisation: a global LLM pass over all files is
O(N²) and financially dead at scale. Before spending any LLM tokens on judging, we must prove the
**cheap candidate generator** has the recall to be worth judging. This PR builds the generator and an
**audit table** and measures candidate recall/precision — **promoting nothing to edges** (phase 3A).

## Scope

- **Candidate generator (extend the linker):** for cross-domain pairs not already deterministically
  linked, emit *candidates* using cheap signals — embedding similarity, name/path/symbol overlap,
  README/section proximity, code-ownership co-location. Bounded fan-out per artifact (no N²).
- **`relationship_candidate` audit table (migration, forward+rollback):** `from_artifact_id`,
  `to_artifact_id`, `signals` (jsonb: which fired + scores), `candidate_recall_bucket`, `kb_version`,
  created_at. **No edges written.** Candidates are an audit/measurement artifact only.
- **Metrics:** candidate recall (against the cross-domain golden set's expected relations),
  candidate precision (sampled), volume per artifact, and **cost-if-judged** (estimated tokens to
  judge the candidate set) — reported by the eval harness so we can decide phase 3B is affordable.
- Tests: generator is deterministic for fixed inputs; fan-out is bounded; candidates land in the
  audit table and **never** in `knowledge_edge`; recall/precision/cost metrics computed.

## Do NOT

- **Do not write any edge** and do not call an LLM — this PR only generates and measures candidates.
- Do not promote, rank-for-serving, or expose candidates through the broker.

## Acceptance criteria

- [ ] Candidates land in `relationship_candidate` with their signals; `knowledge_edge` is untouched.
- [ ] Fan-out per artifact is bounded (no O(N²)); generator is deterministic.
- [ ] Candidate recall / precision / volume / cost-if-judged reported against the golden set.
- [ ] Migration up/down tested; `make verify` + `make eval-run` green.
