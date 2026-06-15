# PR-34 — Deterministic code `search_text` (ADR-0018 Phase 2)

> Plan only — implements ADR-0018 Phase 2. Phase 1 (PR landed) made `github_code` graphify-only with
> exact-span `body_text`. This PR adds the **deterministic retrieval surface** that closes the
> concept/identifier recall gap the design reviews flagged — **still zero LLM for code.**

## Goal

Make code retrievable by the words people actually search, without summarizing code with an LLM. Per
ADR-0018: `code_symbol.body_text` stays the **exact source span** (citable evidence); add a separate
`search_text` field that the index/search ranks on, built **deterministically** from the AST.

## Why

Raw-code keyword search nails identifier/symbol queries (`validate_token`, `AuthMiddleware`, route
strings) but under-serves concept queries phrased in words that don't appear verbatim in code
(`how does login work?` vs `SessionGate`). The fix is deterministic, not an LLM: surface the
docstrings, comments, split identifiers, signatures, and call/import names that already exist in the
span. (Semantic search + targeted cached summaries are Phase 3 / PR-35.)

## Scope

1. **Schema (migration, reversible):** add nullable `knowledge_artifact.search_text TEXT`. Down =
   drop column. No backfill required — it populates on the next build (idempotent, content-hash
   gated as today).
2. **Extraction (no LLM):** extend the Phase-1 `graphify/span_recovery.py` AST pass (it already
   parses each file) to also collect per symbol:
   - `qualified_name` (e.g. `pkg.module.Class.method`), `kind`, `signature` (from `ast` args),
     `decorators`, `docstring` (`ast.get_docstring`), leading/inline `comments`,
   - `imports` (module/symbol names) and **called symbol names** (`ast.Call` targets) within the span,
   - literal **route strings / log messages / config keys** present in the span,
   - **split identifiers**: tokenize snake_case/camelCase symbol + call names into words.
   Compose these into `search_text` (dedup, stable order). Set it on the `CodeArtifactDraft` and write
   it through `graphify/write.py` to the new column. Python-first; other languages leave it null.
3. **Projection + search:**
   - `indexing/search_document.py` `SearchDoc` gains `search_text`; `indexing/projection.py` selects
     it; a code_symbol is projectable if it has `body_text` OR `search_text`.
   - `mcp-server` `PostgresKeywordSearchClient`: score `search_text` alongside `title`/`body_text`
     (e.g. title 2, search_text 1.5, body_text 1) so a concept-word hit in `search_text` ranks the
     symbol even when the raw body doesn't contain the word. Update `docs/contracts/` if the search
     contract names the scored columns.
4. **Optional (small):** populate `code_file.body_text` with deterministic file-level text
   (import block + top-of-file comment) so file-level imports are searchable. Decide in review.

## Out of scope

- Semantic/embedding search over `search_text`; targeted/cached on-demand LLM summaries — **Phase 3**.
- Non-Python span/metadata recovery (the Phase-1 fallback stays: graph-only until per-language lands).
- Any LLM call for code (still forbidden — invariant of ADR-0018).

## Tests (must add)

- A `github_code`-only build still has `llm_calls == 0` AND now produces non-null `search_text` for
  Python symbols (with split identifiers + docstring words present).
- A **concept query** whose word appears only in the docstring / split identifier (NOT in the raw
  body tokens) retrieves the symbol via `search_text` — proves the recall gain.
- An identifier query still ranks the exact symbol first (no regression).
- Migration up/down round-trips; idempotent re-build doesn't duplicate or re-LLM.

## Risks / decisions for review

- **Where `search_text` lives:** a dedicated column (chosen here) keeps `body_text` pure citable
  evidence vs the retrieval surface — don't fold them.
- **Index size / ranking weights:** `search_text` adds rows of text; tune keyword weights from logs
  (record in ADR if structural, per token-budgets rule).
- **Determinism:** the AST pass must be stable (sorted, deduped) so the same source ⇒ same
  `search_text` ⇒ same `content_hash` behaviour (connectors rule).

## Acceptance

`ruff` + `pyright` clean (both services); migration reversible + noted; the new recall test green and
the identifier-query regression test green; contracts updated if the search columns are documented;
no LLM call on the code path.
