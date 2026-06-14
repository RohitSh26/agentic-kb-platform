# PR-23 — Trust class on edges + trust-aware traversal (`trust_floor`)

## Why

Trust must be enforcement, not decoration (ADR-0011), and it must exist **before** any `INFERRED`
edge is produced (phase 3). This PR adds the `trust_class` column + the bucket vocabulary and makes
`graph.get_neighbors` trust-aware with a default `trust_floor=EXTRACTED`, so the moment inferred
edges arrive the broker already treats them as lower-trust routing hints.

## Scope

- **Migration (forward + rollback):** `knowledge_edge.trust_class TEXT NOT NULL DEFAULT 'EXTRACTED'`
  with a CHECK constraint over the bucket set (`docs/contracts/trust-buckets.md`). Backfill existing
  rows to `EXTRACTED` (all current edges are deterministic). Downgrade drops the column. Index on
  `(kb_version, trust_class)` if traversal profiling needs it.
- **kb-builder:** edge writers set `trust_class` from the producing mechanism + ontology rule
  (deterministic ⇒ `EXTRACTED`). Reject writes with a bucket the mechanism may not assign.
- **mcp-server graph tool:** `graph.get_neighbors` gains `trust_floor` (default `EXTRACTED`) and an
  `include_inferred` flag (default `false`). Returns only edges `>= trust_floor`; `AMBIGUOUS`/
  `REJECTED` never returned; `INFERRED_*` returned only when `include_inferred=true`, labelled as
  routing hints (cannot support a claim). Update the mcp-tools contract + tool schema (versioned).
- Tests: traversal returns only `EXTRACTED` by default; `include_inferred=true` surfaces labelled
  inferred edges; `AMBIGUOUS`/`REJECTED` never returned; ACL + trust filters compose (both enforced).
  Migration up/down test.

## Do NOT

- Do not produce any `INFERRED_*` edges yet (no LLM judge until phase 3) — this PR only establishes
  the column, vocabulary, and read-time enforcement.
- Do not change ACL behaviour beyond composing it with trust filtering.

## Acceptance criteria

- [ ] Migration adds `trust_class` with CHECK + backfill; downgrade reverses it; up/down tested.
- [ ] `graph.get_neighbors` default returns only `EXTRACTED`; `AMBIGUOUS`/`REJECTED` never returned.
- [ ] `include_inferred=true` surfaces inferred edges labelled as non-claim-supporting routing hints.
- [ ] Trust + ACL filters compose; contract + schema versioned.
- [ ] `make verify` green.
