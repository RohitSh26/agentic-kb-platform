# PR-08 — Azure AI Search indexer

## Scope
Project Postgres artifacts into one Azure AI Search hybrid (BM25 + vector) index, behind a SearchClient
interface. Upsert only changed docs.

## Context
docs/architecture §1, §6 (Search is a derived projection), §14 (index-drift risk). ADR-0002.

## Files to create
- `apps/kb-builder/src/indexer/projection.py` (artifact → search doc), `indexer/upsert.py`,
  `packages/common/search/client.py` (SearchClient interface + Azure impl + fake).

## Contracts
Search doc schema (concepts, summaries, chunks, ADO cards, wiki pages, code-symbol summaries) +
embedding fields; embedding_cache records azure_search_doc_id.

## Acceptance criteria
- Index is fully rebuildable from Postgres + source pointers.
- Only changed artifacts are upserted; post-build consistency validation compares index vs registry.

## Required tests
- Projection mapping; upsert-changed-only; consistency check fails on injected drift (using the fake).

## Do NOT
- Let any MCP tool call SearchClient's Azure impl directly later — always through the interface.
- Treat the index as truth.

## Kickoff prompt
"Implement PR-08 behind a SearchClient interface with a fake for tests. Derived, rebuildable,
changed-docs-only upsert, plus a drift consistency check."
