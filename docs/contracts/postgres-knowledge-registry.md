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
| `knowledge_artifact` | Chunks, summaries, concepts, source-backed facts, code artifacts (with spans). `knowledge_kind` ∈ interpreted / source_backed. Carries `acl_teams`, the validity interval (`valid_from_seq`, `invalidated_at_seq`), and the rename link `prior_identity_id`. | kb-builder |
| `knowledge_edge` | Graph edges: `edge_type`, `confidence`, `source` (graphify/linker), `kb_version`, and the validity interval (`valid_from_seq`, `invalidated_at_seq`). The V1 graph store — no graph DB. | kb-builder |
| `generation_cache` / `generation_cache_artifact` | Cache key ⇒ generated outputs ⇒ produced artifacts. | kb-builder |
| `embedding_cache` | Embedding call gate, keyed `(artifact_id, text_hash, embedding_model)`. The vector itself is stored as a float array (`ARRAY(double precision)` — no pgvector in V1), so the Search index rebuilds without re-embedding. | kb-builder |
| `kb_build_run` | One row per nightly build: `kb_version`, `build_seq` (monotonic BIGINT, UNIQUE — the interval-membership cutoff), `status` (`running`/`completed`/`failed`/`validation_failed`/`active`/`superseded`), counters, timestamps. | kb-builder |
| `retrieval_event` | Ledger: one row per MCP retrieval call — `run_id`, `context_pack_id`, `agent_name`, `tool_name`, `status`, `query_text`/`normalized_query`, `retrieval_profile`, `kb_version`, `source_filters`, `returned_artifact_ids`, `reused_evidence_ids`, `new_evidence_ids`, `cache_hit`, `semantic_reuse`, `tokens_returned`, `latency_ms`, `created_at`, `details` (nullable JSONB — per-tool observability payload; see below). | **mcp-server** |

## Columns mcp-server depends on (pinned by its contract tests)

- `kb_build_run.kb_version` (text), `kb_build_run.build_seq` (bigint),
  `kb_build_run.status` (text) — active-version lookup for `/health` and for
  serving evidence. The broker resolves the active build's `build_seq` and serves
  by **interval membership**, not `kb_version` label-equality
  (`version-membership.md`, ADR-0013).
- `knowledge_artifact.valid_from_seq` / `invalidated_at_seq` (bigint) and
  `knowledge_edge.valid_from_seq` / `invalidated_at_seq` (bigint) — the membership
  predicate the broker filters every artifact/edge/provenance/graph/search query
  by: `valid_from_seq <= S AND (invalidated_at_seq IS NULL OR invalidated_at_seq
  > S)` for the active build's `build_seq` `S`. Added by kb-builder migration 0012.
  `knowledge_artifact.prior_identity_id` (uuid) is the rename link (history
  survives a path change).
- `knowledge_artifact.centrality_score` (`float`, nullable; added by kb-builder migration 0019,
  ADR-0028) — a normalized [0,1] graph-centrality (PageRank over `knowledge_edge`) recomputed each
  build. The broker reads it and folds it into its rank key as a transparent multiplicative prior;
  NULL/0 means no graph signal and ranks exactly as before. Derived data, never a citation.
- `retrieval_event` full row shape — written by the Context Broker (PR-10).
- `knowledge_artifact.acl_teams` (`text[]`, NOT NULL, default `'{}'`; added by
  kb-builder migration 0008, also on `source_item`) — the broker's
  `team_acl_v1` filter input. Empty array = org-public (any authenticated
  subject); non-empty = visible only to requesters whose team set intersects.
  An artifact's effective ACL in V1 is its own `acl_teams`; kb-builder propagates
  `source_item.acl_teams` onto derived artifacts at build time — newly written
  derived artifacts inherit it from the writer, and the invalidation pass
  (PR-27 / ADR-0013) propagates an ACL-only change onto a source's live artifacts
  even on a content-unchanged cache hit.

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

## `retrieval_event.details` JSONB — per-tool observability payload (migration 0017)

The `details` column is **nullable JSONB**. It is populated best-effort: a tool
never blocks on observability. Shape is per `tool_name`:

| `tool_name` | Shape |
|---|---|
| `context.create_pack` | `{task, candidates_considered, cards:[{artifact_id,title,score,card_type}], budget:{allowed,used,remaining}}` |
| `context.expand` | `{seed_artifact_ids, tiers:["EXTRACTED",...], edge_types_followed:{<edge_type>:n,...}, cards_added, truncated, tokens}` |
| `context.open_evidence` | `{evidence_id, level, injection_flagged, tokens}` |
| `graph.get_neighbors` | `{artifact_id, depth, trust_floor, neighbors_by_type:{<edge_type>:n,...}}` |
| `context.verify_answer` | `{answer_id, claims:[{claim_id, checks:{...}, ok}], overall}` |
| `governance.checkpoint` | `{from_agent, to_agent, plan_summary, decision, edits}` |

The `governance.checkpoint` event is written by `record_checkpoint()` in the
mcp-server; `tool_name` is `"governance.checkpoint"` and `status` is the gate
decision (`approved`/`edited`/`rejected`/`aborted`). It is not an MCP tool — it
is an internal broker call made by the agent runner (ADR-0021) at each delegation
gate. The `run_id` is the run being gated; `agent_name` is the `from_agent`.

The `replay` CLI (`python -m agentic_mcp_server.replay <run_id>`) loads all
`retrieval_event` rows for a run in `created_at` order and prints a
human-readable action timeline for operator review.
