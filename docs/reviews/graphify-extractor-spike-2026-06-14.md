# Graphify extractor spike — 2026-06-14

Decision record behind ADR-0012. Question (from the external judge): is hand-rolling a multi-language
code extractor wheel reinvention given Graphify (`graphifyy`, safishamsi/graphify) exists?

## Method

- Installed `graphifyy` (PyPI). Core deps are tree-sitter grammars for ~20 languages; LLM backends
  are optional extras, so **code-only extraction needs no API key**.
- Fixture: a 2-file Python package (`pkg/service.py` importing `pkg/util.py`, with intra- and
  cross-file calls and a deliberate name collision: a module-level `helper` import vs a
  `Service.helper` method).
- Ran code-only AST extraction: `graphify update src --no-cluster` (no LLM). Output: 7 nodes, 12 edges.

## Raw output shape (`graph.json`)

- Nodes: `{id, label, file_type, source_file, source_location, _origin}` — **`source_location` is a
  single start line (`"L7"`); there is no span end.** No explicit node kind (file vs symbol inferred
  from `label == basename(source_file)`).
- Edges (`links`): `{source, target, relation, context, confidence, source_file, source_location,
  weight}`. Relations seen: `imports`, `imports_from`, `contains`, `method`, `calls`.

## Findings

| Dimension | Result |
|-----------|--------|
| Symbols + kinds | ✅ file / function / class / method present |
| Start line | ✅ `source_location: "L7"` |
| **Span end** | ❌ not emitted |
| Intra-file calls | ✅ `handle→helper` (L13), `helper→top` (L16) |
| Cross-file call | ✅ `top→util.helper` (L8) resolved across files |
| Cross-file import→symbol | ✅ `service imports util.helper` |
| **False-positive call** | ⚠️ `top→Service.helper` from a pure **name collision** at L8, **also labelled `EXTRACTED`** |
| Vocabulary | ⚠️ `imports_from`, `contains`, `method` — not our ontology |
| Determinism | ✅ stable across runs |
| Install pain | ~30 wheels (tree-sitter grammars); fast; no API key for code |

The false-positive call is the judge's #1 predicted risk, realized on the very first fixture: a
**syntactic name match is not a resolved semantic call**, and Graphify's `EXTRACTED` label cannot be
trusted as our trust class.

## Decision (gate) → ADR-0012

Adopt Graphify as the deterministic extraction backend (its public `extract()` API), behind an
adapter that re-normalizes aggressively:
- map `imports`/`imports_from` → our `imports` (file→file); `calls` → our `calls`; drop structural
  `contains`/`method` (they become artifacts);
- **drop any call site that resolves to >1 target** (ambiguous name collision) rather than store a
  fabricated `EXTRACTED` edge;
- re-derive trust ourselves (never copy Graphify's label);
- phase-1 `code_symbol` artifacts are pointer-style (start line known, exact `span_end` is a tracked
  follow-up).

Do **not** use Graphify's `graph.json`, MCP server, query engine, or report as our system of record —
Postgres stays truth (invariant 1), governed by our broker, ledger, ACL, graph_version, and verifier.
