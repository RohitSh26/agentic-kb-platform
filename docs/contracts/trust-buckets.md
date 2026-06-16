# Contract: Trust buckets

> Cross-service contract. Producers (kb-builder) assign a bucket at creation; the broker
> (mcp-server) enforces it at read time. Versioned with `relation_schema_version`.

## Why buckets, not scores

The judge ruled against decimal confidence: it is uncalibrated and invites false precision. Trust is
expressed as a small set of **buckets that change broker behaviour**. A bucket is derived
deterministically from the producing mechanism and the edge type's evidence rule — never a
free-floating number.

## The buckets

| bucket          | meaning                                                              | produced by                                  | default traversal | can support a cited claim |
|-----------------|---------------------------------------------------------------------|----------------------------------------------|-------------------|---------------------------|
| `EXTRACTED`     | Directly present in a source with a verifiable span / deterministic key | AST extractor, deterministic linker          | included          | yes                       |
| `INFERRED_HIGH` | LLM-judged from bounded candidates, strong evidence quote           | LLM judge (phase 3B)                         | included only if `include_inferred=true` | no — routing hint only |
| `INFERRED_LOW`  | LLM-judged, weaker / partial evidence                               | LLM judge (phase 3B)                         | included only if `include_inferred=true` | no — routing hint only |
| `AMBIGUOUS`     | Candidate the judge could not resolve, or unknown edge type         | LLM judge / fallback                         | **excluded**      | no                        |
| `REJECTED`      | Judged not a real relationship; retained for audit only            | LLM judge                                    | **never returned**| no                        |

## Ordering

`REJECTED < AMBIGUOUS < INFERRED_LOW < INFERRED_HIGH < EXTRACTED`.
`trust_floor=X` returns buckets `>= X` in this order (subject to the `include_inferred` gate for the
`INFERRED_*` buckets, which are returned as labelled routing hints, never as claim support).

## Enforcement points

1. **Build time:** a producer assigns the bucket from its mechanism + the ontology evidence rule.
   The deterministic mechanisms may ONLY assign `EXTRACTED`. The LLM judge may assign any
   `INFERRED_*` / `AMBIGUOUS` / `REJECTED`, never `EXTRACTED`.
2. **Traversal:** `graph.get_neighbors` default `trust_floor=EXTRACTED`; `AMBIGUOUS`/`REJECTED` never
   in default results. `context.expand` honors **identical** `trust_floor` / `include_inferred`
   semantics: EXTRACTED backbone first (phase 1), INFERRED tier second only when
   `include_inferred=true` (phase 2 starting from all phase-1 nodes). `AMBIGUOUS`/`REJECTED`
   are never returned by either tool, whatever the floor or flag.
3. **Verifier:** L0 rejects any cited evidence whose supporting edge is not `EXTRACTED` (or an
   `EXTRACTED`-trust fact). `INFERRED_*` edges can route an agent to evidence but cannot themselves
   be the cited support for a platform-trusted claim.

## Storage

`knowledge_edge.trust_class` (text, NOT NULL, CHECK in the bucket set). Citeable facts carry the
same bucket. Phase 1 introduces the column with only `EXTRACTED` in use; later phases populate the
`INFERRED_*` values.
