# 02 — Implementation tour (build plane, PR-01 → PR-08)

> A guided walk through the code as it exists today. Read
> [01 — Design deep dive](01-design-deep-dive.md) first for the *why*; this document is the *how*
> and *where*. Paths are repo-relative; line numbers drift, so prefer the named symbols.

## Layout at a glance

```
packages/contracts   schemas everything else is written against (frozen pydantic, versioned)
packages/db          SQLAlchemy models + Alembic migrations (the Knowledge Registry)
packages/common      hashing, structured logging, token budgeting
apps/kb-builder      the nightly build: connectors → build engine → wikify/graphify → linker
apps/mcp-server      health stub only — real server lands in PR-09/10
agents/              product runtime agent manifests (served later by MCP; not Claude Code agents)
evals/               empty case directories; harness lands in PR-12
infra/               README describing the lean Azure footprint; no IaC yet
```

Dependency direction is one-way: `apps` depend on `packages`, never the reverse.

## 1. Contracts (`packages/contracts/src/contracts/`)

Every boundary exchanges **frozen pydantic models** (`ConfigDict(frozen=True, extra="forbid")`)
carrying an explicit `schema_version`. Version constants live in `contracts/versions.py`:
`OUTPUT_SCHEMA_VERSION`, `MCP_SCHEMA_VERSION`, `PROMPT_VERSION`, `CHUNKER_VERSION`,
`GRAPHIFY_VERSION` (1.1.0 — bumped when artifact emission changed in PR-06),
`PARSER_CONFIG_VERSION`. These constants are *cache-key inputs*: bumping one deliberately
invalidates the relevant generation cache.

- `artifact_schemas/sources.py` — `SourceType` (github_code | github_doc | azure_wiki | ado_card),
  `SourceRef` (mirrors `source_item`: uri, version, repo/branch/path/external_id), and
  `NormalizedContent` (source + normalized text + `content_hash`; same source state must hash
  identically on any machine).
- `artifact_schemas/wikify.py` — `Chunk`, `ConceptDraft`, `SourceBackedFactDraft`,
  `WikifyGeneration` (the ModelClient response shape), and `WikifyArtifactDraft` with a validator
  forcing `knowledge_kind` to match the artifact type (chunks/facts are `source_backed`;
  summaries/concepts are `interpreted`).
- `artifact_schemas/graphify.py` — `FileGraph` (parsed code file: symbols, endpoints, tests,
  imports, calls), `CodeArtifactDraft` (symbolic `key`, optional snippet + 1-based inclusive
  `span_start`/`span_end`), `CodeEdgeDraft` (symbolic from/to keys + `CodeEdgeType`).
- `artifact_schemas/linker.py` — `LinkEdgeDraft` (UUID from/to, `LinkerEdgeType` ∈ documents |
  implements | requests | mentions, confidence, `strategy` ∈ deterministic | semantic). The
  docstring records the subject-verb-object direction convention.
- `search_schemas/` — the Search projection contract: `SearchDoc` (one index document; identity is
  `doc_id = str(artifact_id)`, stable across rebuilds; carries `artifact_hash` for drift
  comparison and the optional embedding vector), `IndexState` (doc_id → artifact_hash snapshot of
  the live index), and `PROJECTABLE_ARTIFACT_TYPES` (artifacts with body text — pointer-only code
  artifacts are reachable via graph edges, not search).
- `mcp_schemas/` and `agent_output_schemas/` — base models only (`McpModel`,
  `AgentOutputModel`); populated from PR-09 onward.

## 2. The registry (`packages/db`)

Models in `src/db/models/`, one file per table, deterministic constraint naming via
`base.py` so migrations are stable. Tables:

| Table | Purpose | Notable constraints |
|---|---|---|
| `source_item` | One row per external source (file, wiki page, card) | `uq_source_item_source_type_source_uri` — the natural identity all upserts target |
| `knowledge_artifact` | Typed knowledge units | `ck_..._knowledge_kind` check; indexes on content_hash, kb_version, source_id |
| `knowledge_edge` | The graph | `uq_knowledge_edge_linker` partial unique (from, to, edge_type) WHERE source='linker' — one row per logical linker edge |
| `generation_cache` | LLM call gate | PK = deterministic cache_key |
| `generation_cache_artifact` | Ordered cache→artifact mapping | **The** source of truth for cache-hit output sets; `output_artifact_id` on the parent is a denormalized copy of position 0 |
| `embedding_cache` | Embedding call gate + canonical vector store | PK (artifact_id, text_hash, embedding_model); `embedding` holds the vector so the index rebuilds without re-embedding; `azure_search_doc_id` stamped on upsert |
| `kb_build_run` | Build audit + version lifecycle | `uq_kb_build_run_single_active` partial unique on status='active' (invariant 5) |
| `retrieval_event` | Runtime ledger (used from PR-10) | indexes on run_id, normalized_query, kb_version |

Migrations (`packages/db/alembic/versions/`): `0001` creates the registry; `0002` adds source
identity + single-active-build; `0003` adds `generation_cache_artifact` + `knowledge_kind`
(with a backfill of pre-existing cache rows); `0004` adds code spans; `0005` adds the linker
partial unique index; `0006` adds the `embedding_cache.embedding` vector column. Every migration
has a tested downgrade.

## 3. Common (`packages/common`)

- `hashing` — `content_hash` (SHA-256 hex), `normalize_text` (NFC, CRLF→LF, strip trailing
  whitespace/blank edges, exactly one trailing newline), `normalize_code` (line endings **only** —
  code evidence must stay an exact snippet), `normalized_content_hash`. Determinism here is what
  makes the whole incremental build trustworthy.
- `logging` — `get_logger(name)` returns a `key=value` structured logger; handlers are cached;
  bare prints are banned.
- `token_budgeting` — `TokenBudget` dataclass (`max_tokens`, `used_tokens`, `can_spend`). The
  Context Broker (PR-10) enforces it; prompts never do.
- `search/` — the `SearchClient` Protocol (`upsert_docs`, `delete_docs`, `fetch_index_state`),
  the in-memory `FakeSearchClient` (stores full `SearchDoc`s; tests inject drift by mutating it),
  and `azure.py` — the **only** module in the repo allowed to import `azure-search-documents`.

## 4. Connectors (`apps/kb-builder/src/kb_builder/connectors/`)

`base.py` defines two seams:

- `FetchBackend` (Protocol) — `list_sources()` + `fetch_text(source)`; must decode UTF-8 strictly
  so hashes never diverge. Real network backends do not exist yet — they are injected, which is
  also what makes everything testable offline.
- `BaseConnector` — the shared pipeline: fetch raw → `_normalize()` → `content_hash` → structured
  log → `NormalizedContent`.

Concrete connectors are thin subclasses: `GitHubCodeConnector` (version = commit SHA, overrides
`_normalize` with `normalize_code`), `GitHubDocConnector` (commit SHA, full normalization),
`AzureWikiConnector` (page revision; `external_id` carries the page id), `AdoCardConnector` (card
revision; the backend renders card fields deterministically — cards mutate, so we snapshot
normalized fields, per the raw-storage policy).

## 5. Build engine (`apps/kb-builder/src/kb_builder/build/`)

The heart of the platform. Three files:

**`cache.py`** — deterministic cache keys (`\x1f`-joined inputs, SHA-256): `chunk_summary_cache_key`
(source content hash + chunker/prompt/model/schema versions), `code_graph_cache_key` (repo +
commit + path + file hash + graphify/parser versions), `concept_rollup_cache_key` (future).
`GenerationCacheGate.lookup_artifact_ids` reads the ordered mapping table;
`record(...)` is idempotent (`on_conflict_do_nothing`), and a concurrent-builder race on the same
miss is resolved by the unique constraint — the loser aborts rather than double-writing.
`EmbeddingCacheGate` is the same pattern keyed on (artifact_id, text_hash, embedding_model).

**`runner.py`** — `BuildRunner.run(connectors)` implements architecture §7:

1. Insert the `kb_build_run` audit row and **commit it immediately** — a failed build still leaves
   an audit trail.
2. Per source: compute hash → `_is_unchanged()` → skip entirely, or `_process_changed_source()`:
   upsert `source_item` (on the natural-identity constraint) → `_wikify_gated` → `_graphify_gated`
   (github_code only) → `_embed_gated` per artifact → `indexer.upsert_documents` (a `SearchIndexer`
   Protocol; the real implementation is `kb_builder/indexer/upsert.py`'s `SearchDocUpserter`).
3. `_write_pending_edges()` — graphify edges are held until all files in the run are flushed, so
   cross-file references resolve; unresolvable symbolic keys **drop the edge with a warning**
   rather than fabricating a node (invariant 7).
4. `run_linker(...)` — same transaction.
5. Mark the run completed and commit. On any exception: roll back per-source work, record the
   error on the (already committed) audit row, mark it failed.

Gated steps share one shape: build the cache key → `lookup` → on hit, return prior artifact ids
(no model call, no counter increment) → on miss, call the interface, write artifacts (flush, not
commit), record the cache row, increment `llm_calls`/`embedding_calls`.

**`active_version.py`** — `activate_kb_version(session, build_id, validate)` promotes a completed
run to `active` *only if* the `ValidationHook` returns True, demoting the previous active run to
`superseded`; on False the run is marked `validation_failed` and the previous version keeps
serving. The real hook (index-vs-registry consistency) arrives with PR-08.

## 6. Wikify (`apps/kb-builder/src/kb_builder/wikify/`)

- `chunker.py` — deterministic paragraph-packing chunker, `MAX_CHUNK_CHARS = 4000`; any behavior
  change must bump `CHUNKER_VERSION` because chunk output feeds the cache key.
- `model_client.py` — the `ModelClient` Protocol (`model_name`, `model_params_hash`,
  `generate_wikify(chunks, prompt_version) → WikifyGeneration`). No SDK import anywhere.
- `generate.py` — `WikifyGenerator.wikify(content)` → drafts. Seed scores: chunks 1.0, facts 0.8,
  concepts 0.6, summaries 0.5 (authority — interpreted knowledge ranks below source-backed);
  freshness 1.0 at build time. **Invariant-7 guard**: a `source_backed_fact` whose supporting
  quote does not appear verbatim in the source text is dropped with a warning — inventions are
  never stored.
- `write.py` — `write_wikify_artifacts` inserts drafts as `knowledge_artifact` rows and flushes
  (ids assigned) but never commits; the runner owns the transaction and records the cache row
  *after* a successful write, so a failed write cannot leave a cache entry pointing at nothing.

## 7. Graphify adapter (`apps/kb-builder/src/kb_builder/graphify_adapter/`)

The parser itself is external/deterministic; this adapter validates and persists its output.

- `parse.py` — `parse_file_graph(raw)` validates against the `FileGraph` contract; fails loudly.
- `keys.py` — symbolic keys (`file:{path}`, `sym:{path}::{name}`, `test:{path}::{name}`,
  `endpoint:{path}::{method} {route}`) and `parse_key` to invert them for DB lookup.
- `to_artifacts.py` — symbols/tests carry the exact snippet and 1-based inclusive line span
  (L2 evidence = precise text at a source version); `code_file` and `endpoint` are pointer-only
  (`body_text=None`). Uses `str.split("\n")`, never `splitlines()` (form-feed/unicode separators
  would corrupt spans).
- `to_edges.py` — emits `imports`/`exposed_as` at confidence 1.0, `calls`/`tests` at 0.9 (dynamic
  dispatch and indirect test relationships are less certain).
- `write.py` — `write_code_artifacts` returns a `(repo, symbolic_key) → uuid` map (paths are
  repo-relative, so the repo is part of the key); `write_code_edges` resolves keys to UUIDs and
  drops unresolved edges with a warning, returning `(inserted, dropped)`.

## 8. Linker (`apps/kb-builder/src/kb_builder/linker/`)

Connects Wikify concepts to Graphify code. Precision-biased by design — over-linking is the
brief's explicit failure mode.

- `records.py` — `LinkableArtifact` (the projection the linker works on) + artifact/source type
  sets.
- `deterministic.py` — `find_deterministic_links(artifacts)`. Exact textual evidence only: a
  symbol's qualified name, file path, endpoint title, or concept title appearing **verbatim**
  (word-boundary regex) in another artifact's text. Guards: concept titles must be multi-word or
  ≥ 6 chars; symbol names ≥ 4 chars; symbol boundaries exclude identifier chars *and dots* so
  `get_user` never matches inside `Service.get_user_embedding`; path boundaries exclude `/` so
  `src/a/b.py` never matches inside `other/src/a/b.py`. One compiled pattern per title
  (`_Matcher`) plus a substring prefilter keeps the docs × titles scan cheap. Produces
  `implements` (0.95), `documents`/`requests` (0.9), `mentions` (0.9).
- `semantic.py` — fallback for concepts the deterministic pass could not link, behind the
  `SimilarityProvider` Protocol. Accepts similarity ≥ 0.82 as `implements` with the **raw score**
  as confidence. No real provider exists until the Azure Search projection (PR-08); the build
  passes `None` and the pass is skipped with a structured log.
- `write_edges.py` — **reconcile-in-place**: upserts target the partial unique index, so a rerun
  refreshes confidence/kb_version on the same row (`(xmax = 0)` distinguishes insert from refresh);
  edges absent from the computed set are deleted (`reason=evidence_gone`); edge types whose
  producing pass was skipped this run are protected from deletion (`protected_edge_types` —
  `run_linker` protects `implements` when the semantic provider is absent, because skipped-pass
  absence is not evidence of staleness). Edges below 0.9 confidence are written but flagged
  (`event=linker_low_confidence_edge`) for the eval harness.
- `run.py` — orchestration. Scans **all** non-deleted artifacts (not just this build's), because
  cache-hit artifacts keep their original kb_version. Returns `(inserted, refreshed, deleted)`.

## 9. Search indexer (`apps/kb-builder/src/kb_builder/indexer/`)

Projects the registry into Azure AI Search and keeps the projection honest (invariant 1: Search is
derived, never truth).

- `projection.py` — `load_search_docs(session, artifact_ids=None)`: a pure read of Postgres
  (artifact + source pointer + cached embedding vector). `artifact_ids=None` is the full-rebuild
  path; passing ids is the nightly changed-only path. The embedding join is keyed on
  `(artifact_id, text_hash == content_hash(body_text))`, so a stale vector from an older body
  never attaches to a newer document.
- `upsert.py` — `SearchDocUpserter` implements the runner's `SearchIndexer` Protocol: only changed
  artifacts are upserted (invariant 4's cost discipline extends to the index), and
  `azure_search_doc_id` is stamped back on the embedding rows. `delete_orphaned_docs` removes
  index documents whose artifact left the registry — without it, an orphan would fail validation
  forever and deadlock activation.
- `consistency.py` — `validate_index_consistency` compares the full projection against
  `fetch_index_state()` and classifies drift as **missing** (in registry, not in index),
  **orphaned** (in index, not in registry), or **drifted** (`artifact_hash` mismatch), each logged
  `event=index_drift class=...` at ERROR. `make_consistency_validator(client)` binds it into the
  `ValidationHook` shape `activate_kb_version` expects — this is the real validation gate behind
  invariant 5.

## 10. What does not exist yet

- `apps/mcp-server` — a health stub. Context Broker, MCP tools, budgets, ledger: PR-09/PR-10.
- Real connector backends (network I/O), agent manifests' runtime, eval harness, IaC.

## 11. Reading order for a new dev

1. `docs/architecture/00-overview.md` (15 min) — the blueprint.
2. This guide's doc 01 — the invariants and why.
3. `packages/contracts/src/contracts/artifact_schemas/` — the vocabulary.
4. `apps/kb-builder/src/kb_builder/build/runner.py` top-to-bottom — the spine everything hangs on.
5. One enrichment layer end-to-end (suggest wikify: chunker → generate → write → its tests).
6. `apps/kb-builder/tests/test_build_engine.py` — the executable spec for the engine's guarantees.
