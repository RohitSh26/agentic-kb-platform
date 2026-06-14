# Contract: Relation ontology (knowledge_edge edge types)

> Cross-service contract. Owned conceptually by kb-builder (producer) and consumed by mcp-server
> (graph tools). Versioned: `relation_schema_version = 1`. Changing evidence rules or trust class
> for an edge type bumps the version and is part of the relationship-judgment cache key.

## Why this exists

The judge's ruling: **trust tags must be enforcement, not decoration, and a strict ontology is
required — no generic `related_to`.** Each edge type declares (a) the evidence required to create
it, (b) its trust class, and (c) whether it may support a final cited claim. The broker enforces
(b) and (c) at read time; the producers enforce (a) at build time.

## Allowed edge types

`edge_type` is a closed vocabulary. Producers MUST reject any value not in this table; the broker
MUST treat an unknown `edge_type` as `AMBIGUOUS` (excluded from default traversal).

| edge_type     | direction (from → to)        | required evidence                                                              | default trust class | can support a claim? |
|---------------|------------------------------|--------------------------------------------------------------------------------|---------------------|----------------------|
| `imports`     | file/module → module         | AST import statement, with source span                                         | `EXTRACTED`         | yes                  |
| `calls`       | symbol → symbol              | AST call site resolved to a definition, with source span                       | `EXTRACTED`         | yes                  |
| `inherits`    | class → class                | AST base-class reference, with source span                                     | `EXTRACTED`         | yes                  |
| `exposes`     | symbol → endpoint/route      | AST decorator/route binding, with source span                                  | `EXTRACTED`         | yes                  |
| `tests`       | test symbol → symbol         | AST reference from a test module to the symbol under test, with span           | `EXTRACTED`         | yes                  |
| `documents`   | doc artifact → code artifact | deterministic ref (path/symbol/anchor) **or** judged prose→code (lower trust)  | `EXTRACTED`/`INFERRED_*` | yes (if EXTRACTED) |
| `implements`  | code/PR → work-item          | deterministic work-item-ID / PR / commit / branch reference                    | `EXTRACTED`         | yes                  |
| `mentions`    | artifact → artifact          | verbatim identifier match (name/path/work-item-ID) found in source text        | `EXTRACTED`         | yes                  |

**Banned:** `related_to` and any other generic catch-all. If a relationship cannot be expressed as
one of the above with its required evidence, it is **not created** — it becomes a candidate
(phase 3 audit table) or an open question, never an edge.

## Trust class rules

- An edge created by the **deterministic** mechanism (AST extractor, deterministic linker) is
  `EXTRACTED`.
- An edge created by the **LLM judge** (phase 3B) over a bounded candidate set is `INFERRED_HIGH`,
  `INFERRED_LOW`, or `AMBIGUOUS` per `docs/contracts/trust-buckets.md` — **never** `EXTRACTED`.
- `documents` is the one type produced by either mechanism; its trust class reflects the producer,
  and an `INFERRED` `documents` edge is a routing hint only (cannot support a claim).

## Required edge fields (maps to `knowledge_edge`)

Every edge row MUST carry: `edge_type` (from this table), `from_artifact_id`, `to_artifact_id`,
`trust_class`, `source` (mechanism: `ast` | `linker_deterministic` | `llm_judge`),
`relation_schema_version`, `kb_version`, and an evidence pointer (artifact id + source span, or the
deterministic match key). Edges without a valid evidence pointer MUST NOT be written.

## Consumption (broker)

- `graph.get_neighbors(trust_floor=EXTRACTED)` returns only edges at or above the floor.
- An edge whose `can support a claim` is "no" (any `INFERRED`/`AMBIGUOUS` edge) may be returned as a
  **routing hint** but MUST be labelled so the verifier rejects it as direct claim support.
- Unknown / banned `edge_type` ⇒ treated as `AMBIGUOUS`, excluded from default traversal.
