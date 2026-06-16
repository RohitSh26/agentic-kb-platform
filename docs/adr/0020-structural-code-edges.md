# ADR-0020 — Structural code edges: `defined_in` (symbol→file) and `imports` (file→file)

- Status: Accepted
- Date: 2026-06-16
- Deciders: Rohit Sharma
- Related: ADR-0012 (Graphify extractor), ADR-0018 (code is graphify-only), ADR-0019
  (semantic linker), `graphify/graphify_backend.py`, `graphify/to_edges.py`

## Context

After enabling the semantic linker + judge (ADR-0019) prose→code reachability rose from 2.4%
to ~41%. But the **code side of the graph has no skeleton**: measured on a real build, all 166
`code_file` nodes are isolated (0 edges) and a `code_symbol` is not linked to the file it lives
in. The only code edges are `calls` (symbol→symbol). So an agent that reaches a symbol cannot
navigate to "the rest of that file" or "the files this file depends on" — it would have to read
the whole raw file, which defeats the purpose of the knowledge base.

Two structural relationships are missing, and both were being dropped, not absent from the data:

1. **symbol → file** ("defined_in"): every symbol already knows its file (it is in the symbol's
   key), but the per-file extractor maps structural containment to artifacts, not edges, and never
   emits the edge. ADR-0012's ontology deliberately dropped `contains`/`method` as edges.
2. **file → file** ("imports"): a file's imports point at *other* files, but per-file extraction
   sees only one file, so the import target is "external" (no node in that file's graph) and is
   dropped. Cross-file resolution never happens.

This is a **generic platform gap**, not specific to one repository: every build of every repo/KB
has it. It must be fixed in the extractor/linker, never worked around per-KB.

## Decision

Materialize both as **deterministic, EXTRACTED-trust** code edges (no LLM, no inference):

1. **`defined_in` (symbol → file).** Add `defined_in` to the `CodeEdgeType` vocabulary and emit one
   edge per code_symbol → its code_file inside `map_extraction`. Pure AST fact (the file path is
   the symbol's own key), per-file, so it fits the incremental build with no new state and no
   migration. Confidence 1.0, `trust_class=EXTRACTED`. This gives every file a role (a hub of its
   symbols) and lets an agent hop symbol↔file↔sibling-symbols and pull only the relevant spans
   instead of the whole file.

2. **`imports` (file → file).** `imports` is already in `CodeEdgeType` but never survives per-file
   extraction. Recover each file's imported module dotted-names deterministically in the existing
   `ast` pass (`span_recovery`), carry them on the build, and resolve them to file nodes by
   path-suffix match against the build's known code files; emit `imports` file→file edges for the
   matches. Unresolved (third-party/stdlib) imports are dropped — never written dangling (the
   no-dangling-citations gate stays green). Python-first; other languages emit no `imports` until
   per-language recovery lands. Confidence 1.0, `trust_class=EXTRACTED`.

No DB migration is required: `knowledge_edge.edge_type` is free text (no CHECK), and the new edges
reuse the existing graphify write path (`trust_class=EXTRACTED`).

## Consequences

- code_file nodes stop being islands; isolated-node count drops sharply. An agent can navigate the
  code structure through the graph (the KB's purpose) instead of reading raw files.
- These are the highest-trust edges in the graph (deterministic AST facts), so the broker can
  prefer them over INFERRED links during traversal.
- `defined_in` ships first (the symbol↔file navigation that directly answers the "don't read whole
  files" need); `imports` follows (cross-file dependency navigation). Both are generic.
- Determinism is preserved: same source ⇒ same edges ⇒ stable content-hash behaviour (connectors
  rule), so incremental rebuilds neither duplicate nor churn these edges.

## Alternatives considered

- **Run whole-repo Graphify to get its cross-file/community edges**: rejected as the authoritative
  source (ADR-0019) — bypasses our trust contract; per-file extraction + deterministic resolution
  keeps truth in our model and the build incremental.
- **Leave files reachable only via prose/judge edges**: rejected — that is inferred, lower trust,
  and leaves the code skeleton (file membership, dependencies) absent; structure should be a
  deterministic fact, not an LLM guess.
- **Store file imports in a new column**: rejected as unnecessary — the AST pass already parses each
  file, so imports are recovered + resolved within the build with no schema change.
