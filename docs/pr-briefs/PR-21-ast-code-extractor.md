# PR-21 — Deterministic Python AST code extractor (FileGraph producer)

## Why

Design A's strongest leg is unbuilt: `parse_file_graph` only *validates* a `FileGraph` dict — nothing
produces one from source (even `SpyGraphifier` in tests hand-writes the graph). Without a real
extractor there is no code structure in the graph, so the platform cannot answer "where is X defined
/ what calls Y". This is the first build of phase 1 (ADR-0010): deterministic, zero-LLM,
`EXTRACTED`-trust only.

## Scope

- **`graphify/ast_python.py`** — a concrete `Graphifier` that parses one language (Python, via the
  stdlib `ast` module) into a `FileGraph`: symbols (functions/classes/methods) with **source spans**,
  `imports`, and **basic intra-file `calls`** (call sites resolved to a definition in scope). Defer
  endpoints, tests, and inheritance to later (ontology lists them; this PR ships symbols/imports/
  basic calls/spans ONLY, per the judge's "thin exact slice").
- Map AST output to artifacts/edges via the existing `to_artifacts`/`to_edges`/`write` path. Every
  edge gets `edge_type` from the ontology (`imports`/`calls`), `trust_class=EXTRACTED`,
  `source=ast`, `relation_schema_version=1`, and an evidence pointer (artifact id + span). Reject any
  edge type not in `docs/contracts/relation-ontology.md`.
- Deterministic: same file bytes ⇒ same `FileGraph` ⇒ same `content_hash` (connectors rule). No
  ordering nondeterminism (sort symbols/edges).
- Unit tests (hermetic, no DB): a handful of real Python snippets → asserted symbols/spans/imports/
  calls; determinism (parse twice, identical); malformed/syntax-error file ⇒ recorded as an
  extractor error (counts toward the publish-gate error rate), not a crash.

## Do NOT

- No LLM calls. No second language. No endpoint/test/inheritance inference (later PRs).
- Do not write edges without a valid evidence pointer or with a banned/unknown `edge_type`.
- Do not touch the broker, fetch backends, or the build CLI (PR-22 wires it in).

## Acceptance criteria

- [ ] A Python file produces symbols (with spans), `imports`, and intra-file `calls` as `EXTRACTED`
      edges through the existing write path.
- [ ] Re-parsing identical bytes yields byte-identical artifacts/edges (determinism test).
- [ ] A syntax-error file is counted as an extractor error, not a crash.
- [ ] All edges carry `edge_type` ∈ ontology, `trust_class=EXTRACTED`, `source=ast`, evidence pointer.
- [ ] `make verify` green (ruff + pyright strict on `graphify` + tests).
