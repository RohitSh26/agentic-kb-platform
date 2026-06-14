# ADR-0012 — Adopt Graphify (`graphifyy`) as the multi-language code-extraction backend

## Status

Accepted (2026-06-14). Amends ADR-0010 (the extractor split): the deterministic code extractor is
**Graphify**, not a hand-rolled parser.

## Context

ADR-0010 calls for a deterministic, zero-LLM code extractor that turns source into our graph. The
first plan was a stdlib-`ast` extractor (Python-only). An external judge (2026-06-14) flagged that
hand-rolling a *long-term multi-language* extractor is wheel reinvention, because **Graphify**
(`graphifyy` on PyPI; safishamsi/graphify) already does tree-sitter AST extraction across ~20
languages locally, with no API key for code. The judge's instruction: spike Graphify as a *producer*
first; keep stdlib `ast` only as a contingency; **never** adopt Graphify's `graph.json`/MCP/query/
report as our system of record, and **re-normalize its output aggressively** — its `EXTRACTED` label
is not our trust class, and a syntactic call site is not a resolved semantic call.

We ran the spike (`docs/reviews/graphify-extractor-spike-2026-06-14.md`). Findings on a 2-file Python
fixture (code-only `graphify update --no-cluster`, no LLM):

| Dimension | Result |
|-----------|--------|
| Symbols + kinds | ✅ file / function / class / method nodes, correct |
| Start line | ✅ present (`source_location: "L7"`) |
| **Span end** | ❌ **not emitted** — single start line only |
| Intra-file calls | ✅ correct (`handle→helper`, `helper→top`) |
| Cross-file calls | ✅ `top→util.helper` resolved across files (our stdlib slice deferred this) |
| Cross-file import→symbol | ✅ `service imports util.helper` |
| **False-positive call** | ⚠️ `top→Service.helper` emitted by **name collision** at the same call site, **also labelled `EXTRACTED`** — the judge's #1 risk, realized immediately |
| Vocabulary | ⚠️ `imports_from`, `contains`, `method` — not our ontology |
| Languages | ✅ ~20 via tree-sitter; incremental `update`/`watch`; no API key for code |

## Decision

1. **Graphify is the deterministic code-extraction backend.** We depend on `graphifyy` and call its
   **public Python API** (`from graphify import extract, collect_files`) — `extract(paths) -> dict`
   is pure AST, no LLM. We do **not** shell out to its CLI in the build path, and we do **not** use
   its `graph.json` file, MCP server, query engine, or report as truth.

2. **An adapter (`graphify_backend.GraphifyGraphifier`) re-normalizes Graphify output into our
   model** — it implements the existing `Graphifier` protocol and returns `GraphifyResult`
   (artifacts + edges). Normalization rules (enforced in the adapter, asserted by tests):
   - **Vocabulary → our ontology** (`docs/contracts/relation-ontology.md`): `imports`/`imports_from`
     → `imports`; `calls` → `calls`. `contains`/`method` are structural (file↔symbol, class↔method)
     and become artifacts, not edges. Any relation outside our ontology is dropped.
   - **Trust is re-derived, never copied.** Graphify's `EXTRACTED` label is ignored. A relation is
     stored as `EXTRACTED` only when it is unambiguous; see the next rule.
   - **Name-collision calls are dropped, not stored.** When a single call site
     (`source_file`+`source_location`) resolves to **more than one** target, the resolution is
     ambiguous (syntactic name match, not a semantic call). We drop **all** of that call site's
     edges from the `EXTRACTED` graph rather than fabricate a wrong `calls` edge. (Phase 3 may later
     re-introduce such pairs as candidates for the LLM judge — never as `EXTRACTED`.)
   - **Spans:** Graphify emits only a start line, so phase-1 `code_symbol` artifacts are
     **pointer-style** (`span_start` known, no exact-snippet body). Recovering exact `span_end` for
     precise L2 snippets is a tracked follow-up (recover from tree-sitter end points or a span pass).

3. **Postgres stays truth (invariant 1).** The adapter writes through the existing
   `write_code_artifacts` / `write_code_edges`; Graphify never persists anything.

4. **Stdlib `ast` is NOT kept in the tree.** Per the owner's directive ("use Graphify's modules,
   don't write extra AST code"), the hand-rolled extractor is removed. The judge's "keep a fallback"
   is recorded here as a contingency: if Graphify proves unviable, a stdlib-`ast` producer can be
   reintroduced behind the same `Graphifier` protocol.

## Consequences

- New runtime dependency: `graphifyy` (+ ~30 transitive packages, mostly tree-sitter grammars) in
  **kb-builder only**. mcp-server is unaffected (it never extracts).
- We get ~20 languages + cross-file resolution "for free," but inherit Graphify's output quirks
  (no span end, name-collision calls), handled by the adapter's normalization — which is exactly the
  judge's "re-normalize aggressively" requirement.
- Tests are hermetic: the pure mapping (Graphify dict → our model) is tested against a captured
  fixture `graph.json`; a live-extraction test is skipped where `graphify` is unavailable.

## Alternatives rejected

- **Hand-rolled multi-language extractor:** wheel reinvention (judge).
- **Use Graphify's `graph.json` / MCP / query as the platform:** wrong architecture — no ACL, no
  graph_version, no ledger, no verification (judge). We use only `source bytes → extraction dict`.
- **Trust Graphify's `EXTRACTED` label verbatim:** would import false-positive calls as truth.

## Follow-ups

- Recover exact `span_end` for `code_symbol` snippets (tree-sitter end points or a span pass).
- Feed dropped name-collision call sites to the phase-3 candidate table (not the `EXTRACTED` graph).
- Pin `graphifyy` version; track its output-schema changes against our adapter with a contract test.
