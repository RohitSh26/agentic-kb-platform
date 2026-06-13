# Contract: Azure AI Search index

> The index is a **derived, rebuildable projection** of the Postgres Knowledge
> Registry — never truth. A full rebuild from Postgres must reproduce identical
> document identities. No service may write truth only to Search.

## Ownership

| Concern | Owner |
|---|---|
| Index population (upserts, orphan deletes), drift validation | **kb-builder** (`indexing/`) |
| Query-time retrieval (hybrid BM25 + vector) | **mcp-server** Context Broker (PR-10) |

Both services access Search behind their own `SearchClient` interface; neither
calls the Azure SDK from business logic. Each service carries its own copy of
the interface (duplication over coupling).

## Document schema (V1)

Code authority: `services/kb-builder/src/agentic_kb_builder/indexing/search_document.py`
(`SEARCH_SCHEMA_VERSION = "1.0.0"`).

| Field | Type | Notes |
|---|---|---|
| `doc_id` | string, key | `str(artifact_id)` — stable across rebuilds |
| `artifact_id` | UUID | FK back to `knowledge_artifact` (the truth) |
| `artifact_type` | string | concept / summary / chunk / source_backed_fact / code_symbol |
| `source_type` | string | github_code / github_doc / azure_wiki / ado_card |
| `source_uri` | string | provenance pointer |
| `title` | string? | |
| `body_text` | string | the only servable text in the index |
| `kb_version` | string | build that produced the doc |
| `knowledge_kind` | string? | interpreted / source_backed (ranking signal) |
| `authority_score` | float? | ranking signal |
| `freshness_score` | float? | ranking signal |
| `artifact_hash` | string? | for post-build index↔registry consistency checks |
| `embedding` | float vector? | hybrid retrieval |
| `embedding_model` | string? | |

Only artifacts meant to rank by their own text are projected
(`PROJECTABLE_ARTIFACT_TYPES`: concept, summary, chunk, source_backed_fact,
code_symbol). `code_file` and `endpoint` are pointer-only (`body_text` null).
`test` carries a snippet body but is deliberately excluded from the index and
reached through graph edges instead.

## Rules

- kb-builder upserts only changed artifacts (incremental build) and deletes
  orphaned docs; the consistency check compares `doc_id -> artifact_hash`
  against the registry before a `kb_version` can go active.
- mcp-server filters every query by the active `kb_version` and treats all
  retrieved text as untrusted content.
- Schema changes bump `SEARCH_SCHEMA_VERSION`, update this document, and the
  index must remain rebuildable from Postgres alone.
