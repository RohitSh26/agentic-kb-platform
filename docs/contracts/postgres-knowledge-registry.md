# Contract: Postgres Knowledge Registry

> The only shared interface between the two services is documents like this one.
> There is no shared Python package. If you change a table or column here, update
> the kb-builder migration **and this document in the same PR**, and check the
> mcp-server contract tests that pin the columns it reads.

## Ownership

| Concern | Owner |
|---|---|
| Schema (DDL), Alembic migrations | **kb-builder** (`services/kb-builder/migrations/`) |
| All build-plane writes (sources, artifacts, edges, caches, build runs) | **kb-builder** |
| Runtime writes: `retrieval_event` rows, runtime-created evidence artifacts | **mcp-server** |
| Reads | both (mcp-server reads only what this contract documents) |

mcp-server **never** runs migrations and never alters schema. It connects with a
role that has SELECT on the tables below plus INSERT on `retrieval_event`.

## Invariants

- Postgres is the source of truth. Azure AI Search is a derived, rebuildable
  projection (see `azure-ai-search-index.md`).
- UUID primary keys, `timestamptz` timestamps, explicit FKs.
- A `kb_version` is marked `active` only after validation passes; the partial
  unique index `uq_kb_build_run_single_active` (`kb_build_run` WHERE
  `status = 'active'`) guarantees at most one active row. MCP always serves the
  last successful active `kb_version`.
- Every model/embedding call in the build is gated by a cache row
  (`generation_cache`, `embedding_cache`): cache hit ⇒ no LLM call, no embedding.

## Tables

Authoritative definitions: `services/kb-builder/src/agentic_kb_builder/infrastructure/postgres/models/`
and the migrations. Summary:

| Table | Purpose | Written by |
|---|---|---|
| `source_item` | Source identity (`source_type`, `source_uri`, `source_version`, `content_hash`) + normalized text + `acl_teams`. Drives incremental skip. | kb-builder |
| `knowledge_artifact` | Chunks, summaries, concepts, source-backed facts, code artifacts (with spans). `knowledge_kind` ∈ interpreted / source_backed. Carries `acl_teams`. | kb-builder |
| `knowledge_edge` | Graph edges: `edge_type`, `confidence`, `source` (graphify/linker), `kb_version`. The V1 graph store — no graph DB. | kb-builder |
| `generation_cache` / `generation_cache_artifact` | Cache key ⇒ generated outputs ⇒ produced artifacts. | kb-builder |
| `embedding_cache` | Embedding call gate, keyed `(artifact_id, text_hash, embedding_model)`. The vector itself is stored as a float array (`ARRAY(double precision)` — no pgvector in V1), so the Search index rebuilds without re-embedding. | kb-builder |
| `kb_build_run` | One row per nightly build: `kb_version`, `status` (`running`/`completed`/`failed`/`validation_failed`/`active`/`superseded`), counters, timestamps. | kb-builder |
| `retrieval_event` | Ledger: one row per MCP retrieval call — `run_id`, `context_pack_id`, `agent_name`, `tool_name`, `status`, `query_text`/`normalized_query`, `retrieval_profile`, `kb_version`, `source_filters`, `returned_artifact_ids`, `reused_evidence_ids`, `new_evidence_ids`, `cache_hit`, `semantic_reuse`, `tokens_returned`, `latency_ms`, `created_at`. | **mcp-server** |

## Columns mcp-server depends on (pinned by its contract tests)

- `kb_build_run.kb_version` (text), `kb_build_run.status` (text) — active-version
  lookup for `/health` and for serving evidence.
- `retrieval_event` full row shape — written by the Context Broker (PR-10).
- `knowledge_artifact.acl_teams` (`text[]`, NOT NULL, default `'{}'`; added by
  kb-builder migration 0008, also on `source_item`) — the broker's
  `team_acl_v1` filter input. Empty array = org-public (any authenticated
  subject); non-empty = visible only to requesters whose team set intersects.
  An artifact's effective ACL in V1 is its own `acl_teams`; connectors
  propagate `source_item.acl_teams` onto derived artifacts at build time
  (population is a kb-builder follow-up — the empty default keeps current
  behavior until then).

`ledger.list_retrievals` (see `mcp-tools-contract.md`) maps its response from this
table: `tool` ← `tool_name`, `evidence_ids` ← `reused_evidence_ids` ∪
`new_evidence_ids`, `status` ← `status` (text, NOT NULL, server default
`'approved'`; added by kb-builder migration 0007 — values are the broker's
outcome statuses `approved`/`reused`/`denied`/`needs_human_approval` plus the
ledger-only status `error`, written when a call fails before producing a
response, e.g. unknown handles or no active `kb_version`). On `error` rows the
broker writes the sentinel `"-"` for `run_id`/`kb_version` values it could not
resolve. Evidence ids are artifact UUIDs rendered as strings in V1, which is
why the `*_evidence_ids` columns are UUID arrays.

Renaming or retyping these requires a coordinated change in both services.
