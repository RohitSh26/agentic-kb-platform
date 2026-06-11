# 02 — Implementation tour (PR-01 → PR-11)

> A guided walk through the code as it exists today. Read
> [01 — Design deep dive](01-design-deep-dive.md) first for the *why*; this document is the *how*
> and *where*. Paths are repo-relative; line numbers drift, so prefer the named symbols.

## Layout at a glance

```
services/kb-builder  the nightly build: connectors → build engine → wikify/graphify → linker →
                     indexing. Owns the registry: SQLAlchemy models + Alembic migrations.
services/mcp-server  the runtime plane: auth, telemetry, tool contracts, health, Context Broker
docs/contracts/      markdown cross-service contracts — the only thing the services share
agents/              product runtime agent manifests (served later by MCP; not Claude Code agents)
evals/               empty case directories; harness lands in PR-12
infra/               README describing the lean Azure footprint; no IaC yet
```

Each service is a self-contained `uv` project (ADR-0008). There is no shared Python code: the
services never import each other, and import-boundary contract tests in each service fail on any
cross-service or legacy root-package import.

## 1. Contracts (`docs/contracts/` + each service's schema modules)

Every boundary exchanges **frozen pydantic models** (`ConfigDict(frozen=True, extra="forbid")`)
carrying an explicit `schema_version`. Build-plane version constants live in
`agentic_kb_builder/domain/schema_versions.py`: `OUTPUT_SCHEMA_VERSION`, `PROMPT_VERSION`,
`CHUNKER_VERSION`, `GRAPHIFY_VERSION` (1.1.0 — bumped when artifact emission changed in PR-06),
`PARSER_CONFIG_VERSION`. These constants are *cache-key inputs*: bumping one deliberately
invalidates the relevant generation cache. (`MCP_SCHEMA_VERSION` lives in the runtime plane:
`agentic_mcp_server/mcp/tool_schemas/base.py`.)

Build-plane schemas, all under `services/kb-builder/src/agentic_kb_builder/`:

- `domain/source_records.py` — `SourceType` (github_code | github_doc | azure_wiki | ado_card),
  `SourceRef` (mirrors `source_item`: uri, version, repo/branch/path/external_id), and
  `NormalizedContent` (source + normalized text + `content_hash`; same source state must hash
  identically on any machine).
- `domain/wiki_artifacts.py` — `Chunk`, `ConceptDraft`, `SourceBackedFactDraft`,
  `WikifyGeneration` (the ModelClient response shape), and `WikifyArtifactDraft` with a validator
  forcing `knowledge_kind` to match the artifact type (chunks/facts are `source_backed`;
  summaries/concepts are `interpreted`).
- `domain/graph_artifacts.py` — `FileGraph` (parsed code file: symbols, endpoints, tests,
  imports, calls), `CodeArtifactDraft` (symbolic `key`, optional snippet + 1-based inclusive
  `span_start`/`span_end`), `CodeEdgeDraft` (symbolic from/to keys + `CodeEdgeType`).
- `domain/link_records.py` — `LinkEdgeDraft` (UUID from/to, `LinkerEdgeType` ∈ documents |
  implements | requests | mentions, confidence, `strategy` ∈ deterministic | semantic). The
  docstring records the subject-verb-object direction convention.
- `indexing/search_document.py` — the Search projection contract (mirrored in
  `docs/contracts/azure-ai-search-index.md`): `SearchDoc` (one index document; identity is
  `doc_id = str(artifact_id)`, stable across rebuilds; carries `artifact_hash` for drift
  comparison and the optional embedding vector), `IndexState` (doc_id → artifact_hash snapshot of
  the live index), and `PROJECTABLE_ARTIFACT_TYPES` (artifacts with body text — pointer-only code
  artifacts are reachable via graph edges, not search).
Runtime-plane schemas, under `services/mcp-server/src/agentic_mcp_server/mcp/`:

- `tool_schemas/` — the Context Broker tool contracts (PR-09), mirrored in
  `docs/contracts/mcp-tools-contract.md`. `base.py` holds `McpModel`
  (frozen, `extra="forbid"`, pinned `schema_version`); `context.py` / `graph.py` / `ledger.py`
  define request+response pairs for all six V1 tools; `evidence.py` defines `EvidenceCard`
  (L0/L1 handle: id, type, title, summary, confidence, authority, `tokens_if_expanded`) and
  `AgentRole`. Policy is encoded in the schema itself: `RequestMoreRequest` requires
  question/why_needed/decision_needed/already_checked/max_tokens (a bare `{"query": ...}`
  fails validation before any broker code runs), denied responses must carry `denial_reason`,
  and `OpenEvidenceResponse` names its payload `untrusted_content`. `tool_registry.py` exposes
  `TOOL_SCHEMAS`, the authoritative tool-name → schema table the server registers from.
- `agent_output_schemas/` — the structured outputs runtime agents must produce (PR-11), mirrored
  in `docs/contracts/agent-output-contracts.md`. See §12.

## 2. The registry (`services/kb-builder` — infrastructure/postgres + migrations)

Models in `src/agentic_kb_builder/infrastructure/postgres/models/`, one file per table,
deterministic constraint naming via
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

Migrations (`services/kb-builder/migrations/versions/`): `0001` creates the registry; `0002` adds source
identity + single-active-build; `0003` adds `generation_cache_artifact` + `knowledge_kind`
(with a backfill of pre-existing cache rows); `0004` adds code spans; `0005` adds the linker
partial unique index; `0006` adds the `embedding_cache.embedding` vector column. Every migration
has a tested downgrade.

## 3. Service-local utilities (the former shared `packages/common`)

Self-contained services mean these are deliberately duplicated where needed (ADR-0008):

- `agentic_kb_builder/domain/content_hasher.py` — `content_hash` (SHA-256 hex), `normalize_text`
  (NFC, CRLF→LF, strip trailing whitespace/blank edges, exactly one trailing newline),
  `normalize_code` (line endings **only** — code evidence must stay an exact snippet),
  `normalized_content_hash`. Determinism here is what makes the whole incremental build
  trustworthy.
- `structured_logging.py` (one copy per service) — `get_logger(name)` returns a `key=value`
  structured logger; handlers are cached; bare prints are banned.
- `agentic_mcp_server/domain/token_budget.py` — `TokenBudget` dataclass (`max_tokens`,
  `used_tokens`, `can_spend`). The Context Broker (PR-10) enforces it; prompts never do.
- `agentic_kb_builder/infrastructure/azure_search/` — the `SearchClient` Protocol
  (`upsert_docs`, `delete_docs`, `fetch_index_state`), the in-memory `FakeSearchClient` (stores
  full `SearchDoc`s; tests inject drift by mutating it), and `azure_search_client.py` — the
  **only** module in the repo allowed to import `azure-search-documents`.

## 4. Connectors (`services/kb-builder/src/agentic_kb_builder/connectors/`)

`source_connector.py` defines two seams:

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

## 5. Build engine (`services/kb-builder/src/agentic_kb_builder/application/`)

The heart of the platform. Three files:

**`cache_gates.py`** — deterministic cache keys (`\x1f`-joined inputs, SHA-256): `chunk_summary_cache_key`
(source content hash + chunker/prompt/model/schema versions), `code_graph_cache_key` (repo +
commit + path + file hash + graphify/parser versions), `concept_rollup_cache_key` (future).
`GenerationCacheGate.lookup_artifact_ids` reads the ordered mapping table;
`record(...)` is idempotent (`on_conflict_do_nothing`), and a concurrent-builder race on the same
miss is resolved by the unique constraint — the loser aborts rather than double-writing.
`EmbeddingCacheGate` is the same pattern keyed on (artifact_id, text_hash, embedding_model).

**`build_runner.py`** — `BuildRunner.run(connectors)` implements architecture §7:

1. Insert the `kb_build_run` audit row and **commit it immediately** — a failed build still leaves
   an audit trail.
2. Per source: compute hash → `_is_unchanged()` → skip entirely, or `_process_changed_source()`:
   upsert `source_item` (on the natural-identity constraint) → `_wikify_gated` → `_graphify_gated`
   (github_code only) → `_embed_gated` per artifact → `indexer.upsert_documents` (a `SearchIndexer`
   Protocol; the real implementation is `agentic_kb_builder/indexing/upsert.py`'s `SearchDocUpserter`).
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

## 6. Wikify (`services/kb-builder/src/agentic_kb_builder/wikify/`)

- `chunker.py` — deterministic paragraph-packing chunker, `MAX_CHUNK_CHARS = 4000`; any behavior
  change must bump `CHUNKER_VERSION` because chunk output feeds the cache key.
- `infrastructure/azure_openai/model_client.py` — the `ModelClient` Protocol (`model_name`,
  `model_params_hash`, `generate_wikify(chunks, prompt_version) → WikifyGeneration`). No SDK
  import anywhere.
- `generate.py` — `WikifyGenerator.wikify(content)` → drafts. Seed scores: chunks 1.0, facts 0.8,
  concepts 0.6, summaries 0.5 (authority — interpreted knowledge ranks below source-backed);
  freshness 1.0 at build time. **Invariant-7 guard**: a `source_backed_fact` whose supporting
  quote does not appear verbatim in the source text is dropped with a warning — inventions are
  never stored.
- `write.py` — `write_wikify_artifacts` inserts drafts as `knowledge_artifact` rows and flushes
  (ids assigned) but never commits; the runner owns the transaction and records the cache row
  *after* a successful write, so a failed write cannot leave a cache entry pointing at nothing.

## 7. Graphify adapter (`services/kb-builder/src/agentic_kb_builder/graphify/`)

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

## 8. Linker (`services/kb-builder/src/agentic_kb_builder/linker/`)

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

## 9. Search indexer (`services/kb-builder/src/agentic_kb_builder/indexing/`)

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

## 10. MCP server base (`services/mcp-server/src/agentic_mcp_server/`)

The runtime plane's skeleton (PR-09): everything *around* the broker, so PR-10 can drop broker
logic into an already-secured, already-observable server.

- `mcp/server.py` — `build_server(auth=..., session_factory=..., search_client=..., settings=...)`
  assembles the FastMCP app. The tool surface is registered **exclusively from `TOOL_SCHEMAS`** —
  a tool cannot exist at the boundary without a versioned contract; fastmcp validates each request
  against the contract model (so a bare `{"query": ...}` already dies here), then
  `mcp/tool_handlers.py` dispatches into the broker with the authenticated subject.
  `create_app()` is the production entrypoint (Entra verifier + engine from env config).
- `auth/entra.py` — the only module that knows Entra ID specifics. Bearer tokens are verified
  via the tenant's public **JWKS** endpoint (fastmcp's `JWTVerifier`), so the server holds no
  client secret at all. The test seam is fastmcp's `TokenVerifier` base class: tests inject a
  `FakeVerifier`; fastmcp wraps the `/mcp` endpoint in `RequireAuthMiddleware`, so requests
  without a valid token get 401 before any tool or middleware runs.
- `telemetry/middleware.py` — one structured line per tool call:
  `event=mcp_request tool=... agent=... run_id=... latency_ms=... status=ok|error`. The agent is
  the verified token's subject (never a client-asserted field); `run_id` is read from the
  request payload when present. Errors are logged and re-raised — no silent failures.
- `health.py` + the `/health` custom route — deliberately unauthenticated readiness:
  200 + the active `kb_version` (invariant 5: MCP serves the last successful active version),
  or 503 with `active_kb_version: null` when no build has been activated yet.
- `config.py` — env-only configuration (`DATABASE_URL`, `MCP_ENTRA_TENANT_ID`,
  `MCP_ENTRA_AUDIENCE`). Identifiers, not secrets.

Tests (`services/mcp-server/tests/`) exercise the boundary at the right layer: auth tests
(`tests/integration/`) go through the real HTTP app in-process (httpx ASGI transport — the
in-process MCP client would bypass auth), tool/telemetry/schema tests (`tests/contract/`) use the
in-process client, and health tests need a Postgres that kb-builder's migrations have already
been run against (`make migrate-test-db`) — mcp-server itself never runs migrations.
`mcp_test_support.asgi_http_client` is a context manager rather than a fixture because the app
lifespan's anyio cancel scope must enter/exit in one task.

## 11. Context Broker (`services/mcp-server/src/agentic_mcp_server/context_broker/`)

The policy layer behind the six tools (PR-10). Identity is always the authenticated session
subject — `agent_name`/`role` request fields are correlation/view data only.

- `pack.py` — `create_pack` retrieves once per run from `task + approved_context_plan`, builds
  L0/L1 evidence cards, and records the query in the pack's dedupe history; `read_pack` is free
  (reuse is the point) and writes a `reused` ledger row.
- `retrieval.py` — the single retrieval path: `SearchClient` (4× oversample) → hydrate from
  Postgres → ACL filter → deterministic rerank (source_backed first, then authority, then search
  score) → top 5 cards. Card cost = `estimate_tokens(title) + estimate_tokens(summary)`.
- `request_more.py` — the contractual outcome order: exact reuse → semantic reuse (token-cosine ≥
  `semantic_reuse_threshold`, default 0.90) → per-agent `denied` (with `denial_reason`) → per-run
  `needs_human_approval` → charged `approved`. Dedupe runs **before** budget denial so an agent
  is never refused evidence the run already paid for.
- `evidence.py` — `open_evidence` is the only road to raw text (L2), by handle only, hydrated
  from Postgres at expansion time, capped by `max_tokens`, and charged against **both** the run
  budget and the per-agent allowance. The response field is named `untrusted_content` on purpose.
- `budgets.py` / `state.py` — server-side enforcement primitives. `EvidencePackState` carries a
  per-pack `asyncio.Lock` that serializes check-then-charge (no TOCTOU double-spend); `PackStore`
  is a bounded in-process cache (FIFO, 256 packs) — the ledger, not the pack, is the durable
  record.
- `dedupe.py` — deterministic normalized-token similarity (no embeddings in the broker, V1).
- `graph.py` — depth/fan-out-capped BFS over `knowledge_edge`; titles + edge metadata only.
- `ledger.py` + `audit.py` — every call writes a `retrieval_event` row, including failures
  (ledger-only status `error`, `"-"` sentinels for unresolved run/kb_version);
  `ledger.list_retrievals` audits itself.
- `infrastructure/search/` + `infrastructure/postgres/keyword_search.py` — `SearchClient` is the
  seam: a Postgres keyword scorer locally, Azure AI Search later behind the same interface.

The executable spec is `tests/integration/test_context_broker.py`: exact + semantic reuse,
per-agent and per-run denial, evidence expansion/truncation, budget-race concurrency, the 5-card
cap, and an injection-style document that must come back verbatim as data without changing any
broker decision.

## 12. Agent manifests + output schemas (`agents/` + `agent_output_schemas/`)

The "controlled specialists" layer (PR-11). `agents/*.md` are the **product's** runtime manifests
(orchestrator, implementation, test_layer, code_reviewer, delivery_planner, pr_planner — not
Claude Code subagents): YAML frontmatter declares `allowed_tools` (context.\*/ledger.\* only,
never search), `max_context_calls` / `max_context_tokens` (must match
`.claude/rules/token-budgets.md`), `requires_evidence_ids`, and an `output_schema` name; the body
is the agent's instruction set. Only the orchestrator may `context.create_pack`.

`services/mcp-server/src/agentic_mcp_server/agent_output_schemas/` holds the schemas those names
resolve against (`AGENT_OUTPUT_SCHEMAS`: `phased_pr_plan_v1`, `implementation_plan_v1`,
`test_plan_v1`, `review_findings_v1`, `delivery_plan_v1`, `pr_plan_v1`), mirrored in
`docs/contracts/agent-output-contracts.md`. Two enforcement layers, both structural rather than
prompt-based:

- **Construction**: every claim-bearing component (`EvidencedClaim`, `ImplementationStep`,
  `PlannedTest`, `ReviewFinding`, `RolloutStep`, `PlannedPr`) requires non-empty `evidence_ids` —
  an unevidenced claim is *unconstructible*; what cannot be proven goes in `open_questions`.
- **Reference check**: `validate_evidence_references(output, known_evidence_ids)` walks the model
  tree (`referenced_evidence_ids`) and raises `AgentOutputValidationError` on any handle the
  Evidence Pack never returned.

Models are frozen with `extra="forbid"` and pin `schema_version` (like the tool schemas).
Executable specs: `tests/contract/test_agent_output_schemas.py` (claims without evidence cannot
exist; unknown IDs fail) and `tests/contract/test_agent_manifests.py` (manifests stay consistent
with `TOOL_SCHEMAS`, the schema registry, and the budget rules).

## 13. What does not exist yet

- Real connector backends (network I/O), the orchestrator runtime that executes the manifests,
  eval harness (PR-12), security hardening (real ACL policy, PR-13), IaC.

## 14. Reading order for a new dev

1. `docs/architecture/00-overview.md` (15 min) — the blueprint.
2. This guide's doc 01 — the invariants and why.
3. `services/kb-builder/src/agentic_kb_builder/domain/` — the vocabulary.
4. `services/kb-builder/src/agentic_kb_builder/application/build_runner.py` top-to-bottom — the
   spine everything hangs on.
5. One enrichment layer end-to-end (suggest wikify: chunker → generate → write → its tests).
6. `services/kb-builder/tests/integration/test_build_engine.py` — the executable spec for the
   engine's guarantees.
