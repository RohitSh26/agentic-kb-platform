# ADR-0010 — Unified `build` entry point with an internal extractor split

## Status

Accepted (2026-06-14)

## Context

The product vision is a **single entry point** that ingests code, docs, ADO cards and PDFs in one
pass into **one queryable graph** served through the MCP Context Broker. Today the build plane has
the pieces (connectors, wikify, linker, indexing, `BuildRunner`) but no product-facing command,
no real fetch backend, and — critically — **no code AST extractor** (`parse_file_graph` only
*validates* a hand-written `FileGraph` dict; nothing produces one from source).

An external judge reviewed two designs:

- **Design A** — keep *separate extraction mechanisms* internally (a deterministic AST extractor for
  code; an LLM extractor, quote-guarded, for prose/cards), unified behind one `build` command.
- **Design B** — one LLM pass over everything ("semantic sweep").

The judge's verdict was unambiguous: **pick A; B is a demo architecture.** A global LLM pass over
every file is O(N²) in relationships and financially dead at ~100-developer scale (~1e12 tokens for
a single sweep). Determinism, cost, and verifiability all favour keeping the mechanisms separate and
only *presenting* them as one command.

## Decision

1. **One product-facing entry point, `build`.** A CLI / job (`python -m agentic_kb_builder.build`)
   is the single way to run a build. It wires connectors → extractors → linker → embed → index →
   validate → activate, exactly as `BuildRunner` already orchestrates. Adopters never call the
   sub-steps directly.

2. **Two extraction mechanisms, never collapsed:**
   - **Code → deterministic AST extractor.** Source text → `FileGraph` (symbols, imports, calls,
     spans) with **zero LLM calls**. Produces `EXTRACTED`-trust facts and edges only.
   - **Prose / cards / PDFs → LLM extractor (wikify).** Quote-guarded against the source span
     (invariant 7). Produces summaries, concepts, and source-backed facts. Gated by
     `generation_cache` so unchanged content never re-calls the model.

   The two run under the one command but are distinct modules with distinct trust classes. We do
   **not** route code through the LLM or prose through the AST parser.

3. **Postgres stays the source of truth (invariant 1).** The "one graph" is
   `knowledge_artifact` + `knowledge_edge` in Postgres, not a file-based `graph.json`. The vision's
   *ideas* are adopted; our storage model is not changed. Azure AI Search remains a rebuildable
   projection.

4. **Cross-domain relationships are built candidate-then-judge (see ADR-0011 and the relation
   ontology), not by a global LLM pass.** The linker is the cheap candidate generator; the LLM only
   ever judges a bounded, retrieved candidate set, and results are cached per content+prompt+model.
   Deterministic work-item/PR/commit links are built **first**; semantic inference comes later and
   is always lower-trust.

5. **Incremental and idempotent (invariants 4, connectors rule).** `content_hash` gates every step;
   unchanged inputs skip extract/embed/index. A re-run produces no duplicate artifacts or edges.

6. **A `graph_version` (the existing `kb_version`) goes active only after validation +
   publish-gate checks pass** (ADR-0011, `docs/contracts/publish-gates.md`). MCP always serves the
   last active version.

## Consequences

- The next build milestone is the **deterministic AST extractor + a real fetch backend + the
  `build` CLI** — A's strongest leg, currently unbuilt — not more LLM linking.
- Extractor outputs carry a **trust class** from creation; the broker can filter on it (ADR-0011).
- The linker is recognised as a real product surface and gets its own relation-specific eval sets
  (golden queries, precision/recall per edge type), not treated as plumbing. The headline risk is
  **underlinking** — real citations that silently miss the key ADR/card — caught by evidence-recall
  as a publish gate.

## Alternatives rejected

- **Single LLM extraction pass (Design B):** cost-prohibitive, non-deterministic, unverifiable.
- **File-based graph output (`graph.json` / `wiki/`):** breaks invariant 1; loses the broker's
  ACL / budget / evidence governance. Postgres-as-rebuildable-truth is strictly stronger.
- **Graph database for the graph:** excluded by ADR-0003; the graph stays in Postgres tables behind
  MCP graph tools.

## Phasing

Realised across phases 0–4 (`docs/pr-briefs/`): 0 contracts (this ADR + ADR-0011 + ontology +
receipt + ACL + eval skeleton + gates); 1 thin exact vertical slice; 2 deterministic cross-domain
links + invalidation; 3 candidate generator then LLM judge; 4 full trust contract.

A publish gate that is not yet applicable to the current phase is **skipped, not failed** — phase 1
ships the phase-1 gates as enforcing and leaves phase-2 gates (relation precision, no-ghost-edges)
inert until their producing mechanism exists (`docs/contracts/publish-gates.md`). Do not make a
later-phase gate enforcing early, or it will block otherwise-valid builds.
