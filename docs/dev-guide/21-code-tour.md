# 21 — Code tour

> **Point in time: 2026-07-07** (migration head `0021`, MCP schema `1.12.0`). A code tour ages;
> this one is dated so it can age honestly. Trust the *structure* — which subsystem lives where,
> what talks to what — over the specifics, and verify any load-bearing detail against the code.

> A guided walk through the code, organized by subsystem. Read
> [20 — Architecture for contributors](20-architecture-for-contributors.md) first for the *why*;
> this document is the *how* and *where*. Paths are repo-relative; line numbers drift, so prefer
> the named symbols. PR/ADR numbers appear as provenance — where a behavior came from — not as a
> reading order.

## Layout at a glance

```
services/kb-builder   the nightly build: connectors → build engine → docify/graphify → linker →
                      alias miner → indexing. Owns the Knowledge Registry: SQLAlchemy models +
                      Alembic migrations (head: 0021).
services/mcp-server   the runtime plane: auth, telemetry, the 12-tool Context Broker surface,
                      tracing, health.
services/review-panel the dev-gated review draft engine (ADR-0031): LangGraph fan-out of the four
                      reviewer lenses → reconcile → one stored draft. Owns only the dedicated
                      `review_panel` schema. Operations: dev-guide 04.
docs/contracts/       markdown cross-service contracts — the only thing the services share
agents/               the 12 product runtime agent manifests (not Claude Code agents)
evals/                dev-only uv project: benchmark cases + harness + the consolidated tiered
                      runner (`run_all.py`, `make eval-all`)
infra/                README describing the lean Azure footprint; no IaC yet
```

Each service is a self-contained `uv` project (ADR-0008). There is no shared Python code: the
services never import each other, and import-boundary contract tests in each service fail on any
cross-service or legacy root-package import.

## 1. Contracts (`docs/contracts/` + each service's schema modules)

Every boundary exchanges **frozen pydantic models** (`ConfigDict(frozen=True, extra="forbid")`)
carrying an explicit `schema_version`. Build-plane version constants live in
`agentic_kb_builder/domain/schema_versions.py`: `OUTPUT_SCHEMA_VERSION`, `PROMPT_VERSION`,
`CHUNKER_VERSION`, `DOC_EXTRACT_PROMPT_VERSION` (ADR-0023 — gates the docify doc-extraction
generation cache), `GRAPHIFY_VERSION` (1.1.0 — bumped when artifact emission changed in PR-06),
`PARSER_CONFIG_VERSION`. These constants are *cache-key inputs*: bumping one deliberately
invalidates the relevant generation cache. (`MCP_SCHEMA_VERSION` lives in the runtime plane:
`agentic_mcp_server/mcp/tool_schemas/base.py`.)

Build-plane schemas, all under `services/kb-builder/src/agentic_kb_builder/`:

- `domain/source_records.py` — `SourceType` (github_code | github_doc | azure_wiki | ado_card |
  git_metadata), `SourceRef` (mirrors `source_item`: uri, version, repo/branch/path/external_id), and
  `NormalizedContent` (source + normalized text + `content_hash`; same source state must hash
  identically on any machine).
- `domain/docify_artifacts.py` (ADR-0023) — `DocArtifactDraft` (one `knowledge_artifact` row,
  field-identical to the retired `WikifyArtifactDraft`) with a validator forcing `knowledge_kind` to
  match the artifact type (facts are `source_backed`; summaries/concepts are `interpreted`), and
  `DocExtractionResult` (the docify pipeline output). Document sources now run through Graphify's LLM
  doc pipeline behind the `docify` adapter; the artifact ROW shapes are unchanged — only the producer
  is.
- `domain/graph_artifacts.py` — the canonical code shapes the Graphify adapter emits:
  `CodeArtifactDraft` (symbolic `key`, optional snippet + 1-based inclusive `span_start`/`span_end`),
  `CodeEdgeDraft` (symbolic from/to keys + `CodeEdgeType`), `GraphifyResult` (artifacts + edges).
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

- `tool_schemas/` — the Context Broker tool contracts, mirrored in
  `docs/contracts/mcp-tools-contract.md` (`MCP_SCHEMA_VERSION` **1.12.0**). `base.py` holds
  `McpModel` (frozen, `extra="forbid"`, pinned `schema_version`); the request+response pairs cover
  all **thirteen** registered tools — the ten governed/graph/ledger tools that grew through PR-33
  (`context.create_pack` with its optional `intent`, `read_pack`, `request_more`, `open_evidence`,
  `expand`, `create_change_pack`, `verify_answer`, `platform_trust`, `graph.get_neighbors` with
  `trust_floor`/`include_inferred`, `ledger.list_retrievals`) plus the three ADR-0025/0030/0031
  additions: **`kb_search`** (1.9.0, PR-37 — the whole request is one `query` string),
  **`get_task_context`** (1.10.0, PR-39), and **`get_review_draft`** (1.11.0, PR-41 — read-only,
  compute-never fetch of a review-panel-computed draft; no budget charge). Policy is encoded in
  the schema itself:
  `RequestMoreRequest` requires question/why_needed/decision_needed/already_checked/max_tokens (a
  bare `{"query": ...}` fails validation before any broker code runs — *for that tool*; `kb_search`
  is deliberately exactly that shape), denied responses must carry `denial_reason`,
  `OpenEvidenceResponse` names its payload `untrusted_content`, and a `verify_answer` request with
  no claims (or any claim with empty `evidence_ids`) fails at the schema boundary.
  `tool_registry.py` exposes `TOOL_SCHEMAS`, the authoritative tool-name → schema table the server
  registers from — twelve entries.
- `agent_output_schemas/` — the structured outputs runtime agents must produce (PR-11), mirrored
  in `docs/contracts/agent-output-contracts.md`. See §12.

## 2. The registry (`services/kb-builder` — infrastructure/postgres + migrations)

Models in `src/agentic_kb_builder/infrastructure/postgres/models/`, one file per table,
deterministic constraint naming via
`base.py` so migrations are stable. Tables:

| Table | Purpose | Notable constraints |
|---|---|---|
| `source_item` | One row per external source (file, wiki page, card) | `uq_source_item_source_type_source_uri` — the natural identity all upserts target |
| `knowledge_artifact` | Typed knowledge units | `ck_..._knowledge_kind` check; indexes on content_hash, kb_version, source_id; `search_text` (0016, ADR-0018 phase 2) and `centrality_score` (0019, ADR-0028) columns |
| `knowledge_edge` | The graph | `uq_knowledge_edge_linker` partial unique (from, to, edge_type) WHERE source='linker'; a second partial unique WHERE source='llm_judge' (PR-29); `trust_class` CHECK in the bucket set (PR-23); `relation_schema_version` + `evidence` (PR-28); `valid_from_seq` / `invalidated_at_seq` (PR-27) |
| `generation_cache` | LLM call gate | PK = deterministic cache_key |
| `generation_cache_artifact` | Ordered cache→artifact mapping | **The** source of truth for cache-hit output sets; `output_artifact_id` on the parent is a denormalized copy of position 0 |
| `embedding_cache` | Embedding call gate + canonical vector store | PK (artifact_id, text_hash, embedding_model); `embedding` holds the vector so the index rebuilds without re-embedding; `azure_search_doc_id` stamped on upsert |
| `kb_build_run` | Build audit + version lifecycle | `uq_kb_build_run_single_active` partial unique on status='active'; `build_seq` BIGINT UNIQUE from the `kb_build_seq` sequence (PR-27); publish-gate result columns + `allow_large_delta` (PR-25) |
| `retrieval_event` | Runtime ledger (used from PR-10) | indexes on run_id, normalized_query, kb_version; `details` JSONB (0017) — the per-tool observability payload |
| `relationship_candidate` | Phase-3A audit artifact (PR-28) | cross-domain candidate pairs + firing `signals` (jsonb); **never served through MCP**, no membership columns |
| `relationship_judgment_cache` | Phase-3B LLM-judge gate (PR-29) | PK on sorted endpoint content hashes + schema/prompt/model versions; a hit ⇒ zero LLM calls |
| `entailment_cache` | L3 verifier gate (PR-31) | keyed on `(claim_hash, evidence_ids_hash, prompt_version, model_version)`; kb-builder owns it, mcp-server reads/writes via raw SQL |
| `doc_extraction_output` / `embedding_output` | Crash-durable model-output cache (ADR-0027, PR-35) | raw model output keyed by content+model identity only (no build-scoped FK), written on a **separate connection** so a crashed build never re-pays tokens; **fail-soft** — a cache error degrades to a paid model call, never a failed build (`infrastructure/postgres/durable_output_cache.py`) |
| `trace_span` | Per-step tracing for the mcp-server-owned graphs (ADR-0032, 0021) | written by mcp-server only (`INSERT`-only sink); operator-queried, never served to agents; see `docs/contracts/tracing.md` |

`knowledge_artifact` also gained `acl_teams` (PR-13), `valid_from_seq` / `invalidated_at_seq` +
`prior_identity_id` (PR-27).

Migrations (`services/kb-builder/migrations/versions/`), each with a tested downgrade:
`0001` creates the registry; `0002` source identity + single-active-build; `0003`
`generation_cache_artifact` + `knowledge_kind` (with backfill); `0004` code spans; `0005` the
linker partial unique index; `0006` the `embedding_cache.embedding` vector column; `0007`
`retrieval_event.status`; `0008` `acl_teams`; `0009` `knowledge_edge.trust_class` (PR-23); `0010`
`kb_build_run` publish-gate columns (PR-25); `0011` `knowledge_edge.relation_schema_version` +
`evidence` (PR-28); `0012` kb_version membership (`build_seq`, the interval columns,
`prior_identity_id`, read-path indexes, PR-27); `0013` `relationship_candidate` (PR-28); `0014`
`relationship_judgment_cache` (PR-29); `0015` `entailment_cache` (PR-31); `0016`
`knowledge_artifact.search_text` (ADR-0018 phase 2, PR-34); `0017` `retrieval_event.details`
(per-tool observability JSONB); `0018` the durable model-output cache tables (ADR-0027, PR-35);
`0019` `knowledge_artifact.centrality_score` (ADR-0028, PR-36); `0020` the four `v_*` dashboard
views (ADR-0014 — `v_retrieval_health`, `v_token_economics`, `v_build_health`,
`v_budget_adherence`); `0021` `trace_span` (ADR-0032). Latest revision is **0021**.

(The review-panel service's `review_panel` schema is deliberately **not** migrated here — it is
bootstrapped idempotently by that service, the one documented Alembic exemption. See §16.)

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
  so hashes never diverge. Backends are *injected*, which is what makes everything testable offline.
- `BaseConnector` — the shared pipeline: fetch raw → `_normalize()` → `content_hash` → structured
  log → `NormalizedContent`.

**Fetch backends — local-FS and the production GitHub/ADO backends (ADR-0015).** There are two
families behind the same Protocol, selected by the `build` CLI's `--backend {local,production}` flag
(default `local`):

- `local_fs.py` (PR-22) — `LocalFsBackend` reads a workspace directory; the whole build plane runs
  with no network and no credentials. `local_fs_backend_factory(workspace, version=...)` is the
  default factory. The local backend can fetch only `github_code`/`github_doc` specs: any
  `azure_wiki`/`ado_card` source in the YAML is **skipped with a warning**
  (`event=source_skipped_not_locally_fetchable`), and token `auth` entries are never resolved
  (`authenticates=False` — no dummy `GITHUB_TOKEN` needed locally).
- `http_client.py` (ADR-0015) — `AsyncHttpClient` wraps `httpx.AsyncClient`, injects the auth
  header, and is the **only** place a real HTTP request is made (the same boundary discipline as
  `SearchClient` / `ModelClient`). It retries on 429 (honouring `Retry-After`) and 5xx with bounded
  backoff, and never logs the header, the token, or a query string. `httpx` is a kb-builder-only
  dependency; mcp-server is untouched.
- `github_rest.py` — `GitHubRestBackend` serves both `github_code` and `github_doc`. It resolves the
  configured branch to a commit **SHA once** (`GET /repos/{owner}/{repo}/branches/{branch}`), then
  lists blob paths via the git trees API and reads each file via the contents API, all at that SHA —
  so `source_version` is the SHA and an unchanged repo re-hashes identically. Auth is
  `Authorization: Bearer <PAT>`. A truncated git tree logs `event=github_tree_truncated` and proceeds
  with the partial listing (a complete walk is a tracked follow-up).
- `ado_wiki_backend.py` — `AdoWikiBackend` (ADO Wiki, ADR-0015): lists pages with
  `recursionLevel=full` and pins `source_version` to the wiki git head.
- `ado_work_item_backend.py` — `AdoWorkItemBackend` (ADO Work Items, ADR-0015): a WIQL query yields
  ids, each work item is fetched with `$expand=fields` and normalized into a deterministic snapshot;
  `source_version` is the work-item `rev`. ADO auth is HTTP Basic with an empty username and the PAT
  as password.
- `production_factory.py` — `production_backend_factory()` dispatches each `SourceSpec` to its
  backend (GitHub/ADO) and raises `SourceConfigError` for an unsupported type; it mirrors
  `local_fs_backend_factory` so `connectors_from_config` is unchanged. A `client_transport` seam lets
  integration tests drive the whole config→connector path hermetically with `httpx.MockTransport`.

**Auth is PAT-via-`token_env` only.** Tokens are referenced by environment-variable *name* in config
(`AuthRef.token_env`), resolved by `resolve_token` at load time, and handed to the backend factory as
a local value — a token value is unrepresentable in a `SourceRef`, a `content_hash`, or a log line.
Managed identity is explicitly **backlog** (ADR-0015 owner decision). V1 stays a nightly batch pull —
no webhooks, no event bus, no streaming.

Concrete connectors are thin subclasses: `GitHubCodeConnector` (version = commit SHA, overrides
`_normalize` with `normalize_code`), `GitHubDocConnector` (commit SHA, full normalization),
`AzureWikiConnector` (page revision; `external_id` carries the page id), `AdoCardConnector` (card
revision; the backend renders card fields deterministically — cards mutate, so we snapshot
normalized fields, per the raw-storage policy).

`git_metadata.py` (PR-26) is the odd one out — it has no `FetchBackend`; it shells out to the
local repo's `git log` / `git show` under the workspace root and emits one deterministic `commit`
artifact per commit (`source_version` = full SHA, `source_uri` = `git:<sha>`). The rendering is
subject + body + a delimited sorted changed-file list, so the same commit always hashes the same
and is skipped on rerun. Commit sources are **zero-LLM**: the build runner branches on
`source_type == "git_metadata"` to write one commit artifact (no docify, no graphify, no
`llm_calls`), still embedded and indexed via the shared deterministic paths. A non-repo workspace
is valid — the connector returns no sources, never an error. The commit artifact's `acl_teams` is
the **intersection** of the changed files' source ACLs (`acl-source-visibility.md`): a derivation
only ever narrows visibility, and zero resolvable inputs deny by default.

**Source configuration (PR-14)** — which sources the nightly build ingests is declared in a
reviewed `sources.yaml` (contract: `docs/contracts/source-config.md`; pinned example:
`services/kb-builder/sources.example.yaml`):

- `domain/source_config.py` — the schema as a pydantic discriminated union on `type`
  (`GithubCodeSourceSpec` / `GithubDocSourceSpec` / `AzureWikiSourceSpec` / `AdoCardSourceSpec`),
  plus `PathFilter`: deterministic gitignore-style globs (`**` spans segments, `*` stays within
  one, exclude wins, default include is everything). `AuthRef.token_env` validates as an
  environment-variable *name* — a token value is unrepresentable in the schema.
- `connectors/config_loader.py` — `load_source_config` (yaml.safe_load → validated models;
  failures name the file and the offending source), `resolve_token` (os.environ at load time;
  configured-but-unset is a hard error *when the backend authenticates* — the local backend passes
  `authenticates=False` and never resolves tokens), `FilteredFetchBackend` (wraps any backend:
  excluded paths are never fetched, hashed, or stored; stamps the source's `acl_teams` onto
  surviving refs), and `connectors_from_config(config, backend_factory)` — the seam where the real
  API backends plug in. `acl_teams` flows config → `SourceRef` → `source_item` on insert and
  update, and the PR-27 invalidation pass propagates it onto live artifacts.

## 5. Build engine + the `build` CLI (`services/kb-builder/src/agentic_kb_builder/application/` + `build.py`)

The heart of the platform. The product-facing entry point is `agentic_kb_builder/build.py`
(ADR-0010) — `python -m agentic_kb_builder.build`. It wires connectors → per-source
extract/embed/index → linker → alias miner → validate → activate exactly as `BuildRunner`
orchestrates; adopters never call the sub-steps. `default_collaborators` are no-cloud: `DocExtractor.from_env()` (docify —
Graphify's LLM doc pipeline; `LLM_PROVIDER` defaults to a local Ollama), the whole-tree Graphify
extractor, a `LocalHashEmbedder`, and the **persistent local JSON search projection** (ADR-0017 —
default `./.kb-local-search-index.json`, printed as `search index : <path>`). Two collaborators
are opt-in by env var: `RELATIONSHIP_JUDGE` (any non-empty value hands the phase-3B judge the chat
model; unset ⇒ candidates are generated but never judged) and `EMBEDDINGS_PROVIDER` (validated —
`ollama` or `openai`, selecting `OllamaEmbedder` or `OpenAIEmbedder` via `embeddings/factory.py`
for the ADR-0019 semantic-linker pass; unset ⇒ that pass is skipped; any other value fails the
build loudly — see [07 §3](07-providers-and-api-keys.md#3-embeddings)).

Flags: `--backend {local,production}` (default `local`; `production` selects the GitHub/ADO
factory of §4), `--validate-only` (config pre-flight only — no DB, no network; exit 0/1),
`--no-activate`, `--no-git-metadata`, `--allow-large-delta`, `--kb-version`, `--version`,
`--index-path` (or `$KB_LOCAL_INDEX_PATH`), `--log-format {timeline,raw,json}` (or `$LOG_FORMAT`).
The `git_metadata` connector is appended **last** so its commit artifacts can resolve
changed-file → code edges against code produced earlier in the same build.

Before anything runs, the build takes the **single-builder advisory lock**
(`application/builder_lock.py`, `pg_try_advisory_lock` on a fixed key, held on a dedicated
connection for the whole build). A second builder logs `event=builder_lock_held` and aborts
immediately (`build aborted: another builder is running`, exit 1) — it never queues.

The application package itself:

**`cache_gates.py`** — deterministic cache keys (`\x1f`-joined inputs, SHA-256): `chunk_summary_cache_key`
(source content hash + chunker/prompt/model/schema versions), `code_graph_cache_key` (repo +
commit + path + file hash + graphify/parser versions), `concept_rollup_cache_key` (future).
`GenerationCacheGate.lookup_artifact_ids` reads the ordered mapping table;
`record(...)` is idempotent (`on_conflict_do_nothing`), and a concurrent-builder race on the same
miss is resolved by the unique constraint — the loser aborts rather than double-writing.
`EmbeddingCacheGate` is the same pattern keyed on (artifact_id, text_hash, embedding_model).
Behind the gates sits the **crash-durable output cache** (ADR-0027, PR-35,
`infrastructure/postgres/durable_output_cache.py`): raw doc-extraction and embedding outputs are
side-committed on a separate connection as they are produced, so a build that crashes *before its
own commit* still never re-pays for those tokens on the rerun. It is fail-soft in both directions —
a read error is treated as a miss (the caller pays the model), a write error is logged and skipped;
a cache problem can never crash the build.

**`build_runner.py`** — `BuildRunner.run(connectors)` implements architecture §7, with
**per-source incremental commits** (ADR-0029, superseding the old end-of-build atomic write):

1. Insert the `kb_build_run` audit row and **commit it immediately** — a failed build still leaves
   an audit trail.
2. Per source: compute hash → `_is_unchanged()` → skip entirely, or `_process_changed_source()`:
   upsert `source_item` (on the natural-identity constraint) → `_docify_gated` → `_graphify_gated`
   (github_code only) → `_embed_gated` per artifact → `indexer.upsert_documents` — then **commit
   that source's work**. Completed knowledge lands in the database as it is produced. On a source
   exception: roll back **only that source's uncommitted work**, count it
   (`counters.extractor_failures`), log `event=build_source_failed source_uri=...`, keep the prior
   generation serving (`_touch_last_seen` — the content hash is not advanced, so the next build
   retries it), and **continue with the next source**. One bad source never aborts the build; the
   `extractor_error_rate` publish gate (1% threshold) decides whether the *version* may activate.
3. `_write_pending_edges()` — graphify edges are held until all files in the run are flushed, so
   cross-file references resolve; unresolvable symbolic keys **drop the edge with a warning**
   rather than fabricating a node (invariant 7).
4. `run_linker(...)`, then the deterministic **alias miner** (`run_alias_miner`, PR-38 — see §8b).
5. Mark the run completed and commit. Activation (below) remains atomic (ADR-0013) — incremental
   *persistence* never means incremental *serving*.

Gated steps share one shape: build the cache key → `lookup` → on hit, return prior artifact ids
(no model call, no counter increment) → on miss, call the interface, write artifacts, record the
cache row, increment `llm_calls`/`embedding_calls`.

**`active_version.py`** — `activate_kb_version(session, build_id, validate)` promotes a completed
run to `active` *only if* the `ValidationHook` returns True, demoting the previous active run to
`superseded`; on False the run is marked `validation_failed` and the previous version keeps
serving. Each run also gets a monotonic `build_seq` (the served interval-membership sequence,
ADR-0013).

**`invalidation.py`** (PR-27) — the identity-over-time pass runs at the **end** of a build, after
all writes and the linker but **before** activation. It reconciles identity so the new version
(by interval membership) never serves a deleted/renamed artifact or a ghost edge, *without* mutating
a row a prior active version still serves (it only ever sets `invalidated_at_seq` NULL→build_seq and
propagates `acl_teams`; no live row is physically deleted). Four ordered sub-passes: rename detection
(content-hash reappears at a new path ⇒ `prior_identity_id` link + edge reattach), deletion sweep
(source no longer listed ⇒ invalidate + retire its cache rows), supersession sweep (content-changed
source ⇒ invalidate its prior-generation rows), and ACL propagation. Idempotent: a rebuild on
unchanged inputs sweeps nothing.

**`publish_gates.py`** (PR-25, `docs/contracts/publish-gates.md`) — composes the phase-1 gates
(index consistency, extractor error rate, symbol-count delta, no-dangling-citations,
edge-evidence-integrity — the last enforces the closed `ALLOWED_EDGE_TYPES` ontology) plus the
now-enforcing `no-ghost-edges` gate into one `ValidationHook` via `make_publish_gate_validator`,
which also wraps `make_consistency_validator`. The first failing, non-overridden gate records which
gate + its measured value on `kb_build_run` and returns False, so activation never happens.
`allow_large_delta` overrides *only* the symbol-count gate. Evidence-recall + ACL-leak are enforced
authoritatively by the evals harness (service boundary), logged as a proxy here.

**`write_commit.py`** (PR-26) — writes the single deterministic `commit` artifact per
`git_metadata` source (zero LLM, zero graphify), embedded and indexed via the shared paths.

## 6. Docify (`services/kb-builder/src/agentic_kb_builder/docify/`, ADR-0023)

The hand-rolled prose-LLM `wikify` pipeline is retired and deleted. Document sources
(`github_doc`/`azure_wiki`/`ado_card`) now run through **Graphify's LLM doc pipeline** behind a thin
`docify` adapter, configured from the same `LLM_*` env as every other model call (Groq/Ollama). The
artifact ROW shapes are unchanged — only the producer is.

- `extract_fn.py` — `make_graphify_doc_extract(...)` registers the Graphify LLM backend in-process
  (from the resolved `LLM_*` endpoint) and calls `graphify.llm.extract_files_direct`, returning
  Graphify's raw doc output.
- `extractor.py` — `DocExtractor` (with `DocExtractor.from_env()`): `extract(content) →
  DocExtractionResult`; the runtime-facing seam the build engine injects.
- `docify_backend.py` — `map_doc_extraction(...)`: a **pure, deterministic, I/O-free** mapper that
  re-derives our trust contract from Graphify's raw output (it never copies Graphify's labels). A
  concept whose supporting sentence is a **verbatim substring** of the source text (same
  whitespace-normalization as the broker's L0 verifier — duplicated here because services may not
  import each other) becomes a citable `source_backed_fact` carrying the quote; otherwise an
  `interpreted` `concept`; the document node becomes an `interpreted` `summary`. Seed scores match
  the retired wikify scores (facts 0.8, concepts 0.6, summaries 0.5 — interpreted ranks below
  source-backed; freshness 1.0 at build time). **Artifacts only** — no concept→concept edges, because
  generic relatedness is banned by the relation ontology (`relation-ontology.md`).
- `write.py` — `write_doc_artifacts` inserts drafts as `knowledge_artifact` rows and flushes
  (ids assigned); the runner owns the transaction and records the cache row
  *after* a successful write, so a failed write cannot leave a cache entry pointing at nothing.

The model call is generation-cache gated (`doc_extract_cache_key`, keyed on
`DOC_EXTRACT_PROMPT_VERSION`), so an unchanged document makes no model call — and the raw output is
also side-committed to the durable cache (§5), so even a crashed build's spend survives.

## 7. Graphify: whole-tree code extraction + adapter (`services/kb-builder/src/agentic_kb_builder/graphify/`)

Code structure is delegated to the **Graphify library** (ADR-0012/0018), run **whole-tree, once per
repo** so it resolves cross-file imports/calls/uses natively (the per-file extractor + hand-rolled
import linker were retired). Zero LLM calls. This package is the thin adapter that re-normalizes
Graphify's output into our versioned, ACL'd artifacts/edges and re-derives trust ourselves (we never
copy Graphify's `EXTRACTED` label):

- `graphify_backend.py` — `graphify_tree(files)` materializes the repo's code files to a temp tree,
  calls the library (`graphify.extract`, `cache_root=root`), and feeds the result to
  `map_extraction` (pure, hermetically testable). Edge types emitted (all `EXTRACTED`): `defined_in`,
  `calls`, `imports`, `inherits`, `uses`, `references`. Name-collision call sites are dropped, not
  fabricated.
- `span_recovery.py` — Graphify reports only a start line, so we recover each Python symbol's EXACT
  span (decorators + docstring + body) with a deterministic `ast` pass for citable L2 evidence and a
  keyword-searchable `search_text` (ADR-0018 phase 2, PR-34 — stored on `knowledge_artifact`, the
  deterministic retrieval surface for pointer-first code). Uses `str.split("\n")`, never
  `splitlines()`.
- `keys.py` — symbolic keys (`file:{path}`, `sym:{path}::{name}`, `test:{path}::{name}`,
  `endpoint:{path}::{method} {route}`) and `parse_key` to invert them for DB lookup.
- `write.py` — `write_code_artifacts` returns a `(repo, symbolic_key) → uuid` map (paths are
  repo-relative, so the repo is part of the key); `write_code_edges` resolves keys to UUIDs and
  drops unresolved edges with a warning, returning `(inserted, dropped)`.

## 8. Linker (`services/kb-builder/src/agentic_kb_builder/linker/`)

Connects Docify concepts to Graphify code. Precision-biased by design — over-linking is the
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
  as confidence. Enabled by the validated `EMBEDDINGS_PROVIDER` env var (ADR-0019 — an
  `EmbeddingSimilarityProvider` over `OllamaEmbedder` or `OpenAIEmbedder`, selected by
  `embeddings/factory.py`); unset ⇒ the build passes `None` and the pass is skipped with a
  structured log; any other value fails the build loudly.
- `write_edges.py` — **reconcile-in-place**: upserts target the partial unique index, so a rerun
  refreshes confidence/kb_version on the same row (`(xmax = 0)` distinguishes insert from refresh);
  edges absent from the computed set are deleted (`reason=evidence_gone`); edge types whose
  producing pass was skipped this run are protected from deletion (`protected_edge_types` —
  `run_linker` protects `implements` when the semantic provider is absent, because skipped-pass
  absence is not evidence of staleness). Edges below 0.9 confidence are written but flagged
  (`event=linker_low_confidence_edge`) for the eval harness.
- `run.py` — orchestration. Scans **all** non-deleted artifacts (not just this build's), because
  cache-hit artifacts keep their original kb_version. Returns `(inserted, refreshed, deleted)`.
- `cross_domain.py` (PR-26) — deterministic, zero-LLM, **explicit-reference-only** cross-domain
  rules, all `EXTRACTED` / `source='linker'` / `strategy='deterministic'`: `implements`
  (commit → work-item, parsed from `AB#123` / `#123` / `GH-123` / `PR #123` in the commit message or
  branch, matched by `external_id` or title), `mentions` (commit → code_file by exact changed-file
  path), and `mentions` (doc → work-item by verbatim id). A bare integer never produces a link.
- `candidates.py` + `run_candidates.py` (PR-28, phase 3A) — the cheap **candidate generator**:
  deterministic, zero-LLM signals (`embedding_similarity` None-safe, `token_overlap`,
  `section_proximity`, `path_colocation`) surface cross-domain pairs, bounded **top-K per artifact**
  (`CANDIDATE_FAN_OUT_K`, default 10) so there is no O(N²) cross-product. Writes only the audit table
  `relationship_candidate` (`relationship-candidates.md`) — **no edge, no LLM**. A pair already
  linked deterministically is excluded.
- `judge.py` + `judgment_cache.py` (PR-29, phase 3B) — the **LLM relationship judge**: the first
  place the model rules on a relationship, over *only* the bounded candidate set — and only when
  the `RELATIONSHIP_JUDGE` env gate is set (unset ⇒ candidates are generated and audited but never
  judged). For each pair it asks the `ModelClient` for a verdict under the closed ontology + trust
  buckets, and the verdict becomes an edge — `INFERRED_HIGH`/`INFERRED_LOW`/`AMBIGUOUS` are written
  (`source='llm_judge'`, with `valid_from_seq` so the broker serves them as routing hints),
  `REJECTED` is cache-only. A `supporting_quote` that is not a verbatim substring of a source span is
  downgraded to `AMBIGUOUS` (invariant 7); the judge may **never** emit `EXTRACTED`. Every call is
  gated by `relationship_judgment_cache` (sorted endpoint hashes + schema/prompt/model versions) — a
  hit makes zero LLM calls; judge edges upsert on the `source='llm_judge'` partial unique index.

## 8b. Alias/reference index (PR-38, ADR-0030)

The deterministic bridge from *natural-language names* to artifacts — what lets `kb_search` /
`get_task_context` resolve "the alias reference index" to the right file with zero LLM calls.
`run_alias_miner` runs as its own build stage after the linker: deterministic mining
(tokenize/scope/n-gram extraction over code symbols, doc-slug extraction over docs),
cross-source aggregation, and `alias_reference` **artifacts** (+ edges to their targets) written
through the same idempotent, incremental-skip discipline as every other stage (never-widened ACLs;
a rerun on unchanged sources writes nothing). The pure resolver (exact → fuzzy → deterministic
tie-break) is what mcp-server's task-context path reads; it degrades gracefully to plain keyword
search on a KB that predates PR-38. Golden accuracy is checked by
`scripts/eval_alias_resolution.py` (25 hand-verified cases, ≥ 80% top-1 target — run by
`bootstrap.sh` stage 5 and `make eval-all` T2).

## 9. Search indexer (`services/kb-builder/src/agentic_kb_builder/indexing/`)

Projects the registry into the search projection and keeps it honest (invariant 1: Search is
derived, never truth). Locally the projection is a persistent JSON file (ADR-0017); in production
it is Azure AI Search behind the same interface.

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

The runtime plane's skeleton (PR-09): everything *around* the broker, so the broker logic drops
into an already-secured, already-observable server.

- `mcp/server.py` — `build_server(auth=..., session_factory=..., search_client=..., settings=...)`
  assembles the FastMCP app. The tool surface is registered **exclusively from `TOOL_SCHEMAS`** —
  a tool cannot exist at the boundary without a versioned contract; fastmcp validates each request
  against the contract model, then `mcp/tool_handlers.py` dispatches into the broker with the
  authenticated subject. `create_app()` is the production entrypoint (Entra verifier + engine from
  env config + the `TraceSink` selection of §11).
- `auth/entra.py` — the only module that knows Entra ID specifics. Bearer tokens are verified
  via the tenant's public **JWKS** endpoint (fastmcp's `JWTVerifier`), so the server holds no
  client secret at all. The test seam is fastmcp's `TokenVerifier` base class: tests inject a
  `FakeVerifier`; fastmcp wraps the `/mcp` endpoint in `RequireAuthMiddleware`, so requests
  without a valid token get 401 before any tool or middleware runs. (The opt-in, loopback-only
  local-dev verifier is ADR-0016 — see dev-guide 01 §"Server configuration reference".)
- `telemetry/middleware.py` — one structured line per tool call:
  `event=mcp_request tool=... agent=... run_id=... latency_ms=... status=ok|error`. The agent is
  the verified token's subject (never a client-asserted field); `run_id` is read from the
  request payload when present. Errors are logged and re-raised — no silent failures.
- `mcp/tool_handlers.py` — the uniform dispatch wrapper that also makes the ledger **complete by
  construction**: any exception a handler has not already ledgered itself (marked by
  `LedgeredToolError`) gets exactly one `error` `retrieval_event` row before re-raising, so a
  crashed call never vanishes from the audit record.
- `health.py` + the `/health` custom route — deliberately unauthenticated readiness:
  200 + the active `kb_version` (invariant 5: MCP serves the last successful active version),
  or 503 with `active_kb_version: null` when no build has been activated yet.
- `config.py` — env-only configuration (`DATABASE_URL`, `MCP_ENTRA_TENANT_ID`,
  `MCP_ENTRA_AUDIENCE` required; see dev-guide 01 §"Server configuration reference" for the full
  table). Identifiers, not secrets.

Tests (`services/mcp-server/tests/`) exercise the boundary at the right layer: auth tests
(`tests/integration/`) go through the real HTTP app in-process (httpx ASGI transport — the
in-process MCP client would bypass auth), tool/telemetry/schema tests (`tests/contract/`) use the
in-process client, and health tests need a Postgres that kb-builder's migrations have already
been run against (`make migrate-test-db`) — mcp-server itself never runs migrations.
`mcp_test_support.asgi_http_client` is a context manager rather than a fixture because the app
lifespan's anyio cancel scope must enter/exit in one task.

## 11. Context Broker (`services/mcp-server/src/agentic_mcp_server/context_broker/`)

The policy layer behind the tools. The surface is **thirteen tools**
(`docs/contracts/mcp-tools-contract.md`, `MCP_SCHEMA_VERSION` **1.12.0**) in three families:

- **The preferred, everyday retrieval surface (ADR-0025 / ADR-0030):** `kb_search` (PR-37) and
  `get_task_context` (PR-39) — simple to call, hard-capped in code, no run/pack ceremony.
- **The governed surface** for citation-grade work: `context.create_pack` / `read_pack` /
  `request_more` / `open_evidence` / `expand` / `create_change_pack` / `verify_answer` /
  `platform_trust`, plus `graph.get_neighbors` and `ledger.list_retrievals`. Demoted from "the
  single retrieval path" to the deliberate provenance path — but fully registered and maintained.
- **The dev-gated review path (ADR-0031):** `get_review_draft` (PR-41) — read-only, compute-never
  fetch of a review-panel-computed draft; not knowledge retrieval, so it carries none of
  `kb_search`'s budget machinery.

Identity is always the authenticated session subject — `agent_name`/`role` request fields are
correlation/view data only (since PR-18 `role` is free-form, charset-guarded because it lands in
audit logs). **Retrieval, graph, and provenance filter by interval membership** (the
`valid_from_seq <= S AND (invalidated_at_seq IS NULL OR invalidated_at_seq > S)` predicate of
`version-membership.md`), not `kb_version` label-equality: the broker resolves the active build's
`build_seq` once and serves every row that is a member of it.

**The shared retrieval machinery:**

- `retrieval.py` — the single retrieval path every search-shaped tool goes through:
  `SearchClient` (4× oversample) → hydrate from Postgres → ACL filter → deterministic rerank.
  Relevance is `search score × temporal/intent weight × (1 + 0.25 × centrality)` — the last factor
  is the **ADR-0028 graph-centrality prior** (PR-36): `knowledge_artifact.centrality_score`, a
  normalized PageRank computed at build time; NULL/0 gives a neutral 1.0 factor, and lifts are
  observable (`centrality_lifted` in the `event=temporal_weight_summary` log). Source-backed ranks
  above interpreted, then authority, then score. Card cost = `estimate_tokens(title) +
  estimate_tokens(summary)`; within-retrieval semantic dedupe collapses near-duplicates before the
  3–5 card cap.
- `temporal.py` (PR-33) — deterministic, zero-LLM `source_kind` + `temporal_state` derivation and
  the transparent, logged intent-aware re-weighting (driven by `create_pack.intent`). A
  ranking/label signal only — independent of the L0 `not_stale` check, never removes historical
  evidence.
- `authorization.py` — the ACL/trust decision objects threaded through every surface.
- `infrastructure/search/` + `infrastructure/postgres/keyword_search.py` — `SearchClient` is the
  seam: a Postgres keyword scorer locally, Azure AI Search later behind the same interface.

**`kb_search` (`kb_search.py`, PR-37):** the whole request is `{"query": ...}`; everything
restrictive happens server-side. A per-**(MCP session, subject)** budget window enforces a dual
hard cap — call count AND cumulative tokens, from the same `MCP_AGENT_ALLOWANCES` map the
`context.*` meter uses — with check-then-charge serialized per window (a parallel burst cannot
sneak past the cap). Tokens are charged for the **exact serialized response** (meter == wire).
When either axis closes, the tool returns empty results with the fixed notice ("KB budget spent —
work with what you have, or read the specific files you still need"), ledgered `denied`, never a
tool error (ADR-0025 §4). On an unexpected crash it **refunds** the charge under the same window
lock (`status=refunded` in the log) before the uniform wrapper ledgers the error. Rows carry the
non-run sentinel `run_id="-"` (`constants.py: NO_RUN_SENTINEL`) and a `details` payload of the
window state (`{session, calls_used, tokens_used, max_requests, max_tokens}`).

**`get_task_context` (`task_context.py` + `task_context_nodes.py`, PR-39, ADR-0030 §2):** one
task description in; resolved scope, blast radius (callers/callees/tests), conventions, and
similar prior changes out — every entity confidence-tiered (`ground_truth | deterministic |
interpreted`) and cited. The backend is a **LangGraph StateGraph of four genuinely parallel
pure-retrieval nodes** (`resolve_scope`, `blast_radius`, `conventions`, `similar_prior_changes`)
joined by `synthesize`, with ONE conditional broadened retry when scope resolves empty — zero LLM
calls at query time. Resolution order: hints → the PR-38 alias index → keyword fallback; genuine
ambiguity returns `ambiguous_candidates` + `open_questions`, never a silent guess; a `calls` edge
is `deterministic` only when the import graph corroborates it (else `interpreted` + `caveat`).
The serialized response is capped at the Evidence-Pack band (default 8k) and trimmed
deterministically from the lowest-value tail (`event=task_context_budget_trim`). Its ledger row
(`run_id="-"`, `status="approved"`) carries per-section counts and `node_latency_ms` in `details`.

**Tracing (ADR-0032, `infrastructure/tracing/trace_sink.py` + `dependencies.py`):**
`select_trace_sink` reads `TRACE_SINK` once at startup (`postgres` default when a DB is
configured; `none` ⇒ null sink; anything else fails the boot) and hands a constructed `TraceSink`
through `BrokerDeps.trace_sink`. `get_task_context` emits one root span + one span per node,
`kb_search` one span per call, into the registry's `trace_span` table (migration 0021) — after the
call's own work is done, fail-soft, never budget-charging. Contract: `docs/contracts/tracing.md`;
operator queries: dev-guide [06](06-observability.md).

**The governed tools:**

- `pack.py` — `create_pack` retrieves once per run from `task + approved_context_plan`, builds
  L0/L1 evidence cards, and records the query in the pack's dedupe history; `read_pack` is free
  (reuse is the point) and writes a `reused` ledger row.
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
  record. Since PR-19, the per-subject allowance map is deployment config: the optional
  `MCP_AGENT_ALLOWANCES` env var (JSON `{subject: {max_requests, max_tokens}}`) is parsed
  fail-fast by `parse_agent_allowances` at boot; unlisted subjects keep the conservative default.
- `dedupe.py` — deterministic normalized-token similarity (no embeddings in the broker, V1).
- `graph.py` + `trust.py` (PR-23) — depth/fan-out-capped **trust-aware** BFS over `knowledge_edge`;
  titles + edge metadata only. `trust_floor` (default `EXTRACTED`) and `include_inferred`
  (default `false`) gate which buckets are returned; every `GraphNeighbor` carries the edge's
  `trust_class` and a `claim_supporting` flag (true only for `EXTRACTED`). `AMBIGUOUS`/`REJECTED`
  are never returned or transited; trust filtering composes with the per-hop ACL filter.
- `ledger.py` + `error_ledger.py` + `audit.py` — every call writes a `retrieval_event` row,
  including failures (ledger-only status `error`, `"-"` sentinels for unresolved run/kb_version);
  `ledger.list_retrievals` audits itself and is **subject-scoped** (a `run_id` is not a grant to
  read a co-agent's evidence ids or spend).

The executable specs: `tests/integration/test_context_broker.py` (exact + semantic reuse,
per-agent and per-run denial, evidence expansion/truncation, budget-race concurrency, the 5-card
cap, verbatim injection round-trip), `tests/unit/test_kb_search_budget.py` (the dual cap),
`tests/integration/test_task_context.py` (scope/blast-radius/ambiguity + the printed p50), and
`tests/integration/test_tracing.py` (+ the fail-soft raising-sink test).

## 11b. The trust contract: verifier ladder, signed receipts, client identity (PR-23/24/30/31/32)

The broker governs retrieval; these surfaces govern an external agent's *answer* (ADR-0011,
`verification-receipt.md`, `trust-buckets.md`). All under
`agentic_mcp_server/context_broker/` + `auth/`:

- `verify.py` — `context.verify_answer` runs the layered verifier and returns a verification
  **receipt**. **L0** (deterministic, mandatory, PR-24): each cited evidence id exists, is in the
  served version, is ACL-visible, appears in the requester's retrieval ledger, is not stale, and is
  supported by an `EXTRACTED` edge (an `INFERRED_*` hint cannot be the sole support). **L1/L2**
  (PR-30, deterministic, no LLM): citation coverage + quote span caps, and typed-fact adjudication
  (`symbol_in_file` / `file_imports_module` / `edge_between`) against a `claim_ledger.py` projection
  over the existing tables — catching a claim that *misreads* real evidence. **L3** (PR-31):
  `entailment.py` runs cached LLM entailment **only** for claims L0–L2 could not adjudicate, gated by
  `entailment_cache` so an unchanged claim makes zero model calls (`EntailmentClient`, local Ollama
  `gemma3:4b` in dev). The verifier performs **no generation** and logs only ids/hashes/outcomes.
- `receipt_signing.py` (PR-31) — signs the receipt with **HMAC-SHA256** over
  `answer_hash + graph_version + client_id + claim_results`; the key is read from an env var by
  *name* (default `VERIFY_SIGNING_KEY`), never a literal. `verify_receipt_signature(...)` lets a host
  validate statelessly (no DB, no re-checks); when no key is configured the receipt is still issued,
  unsigned.
- `auth/client_identity.py` + `auth/scopes.py` (PR-32) — a request carries both the per-user
  `Requester` and a registered `ClientIdentity` (`client_id`, `scopes`, `verification_required`),
  resolved from the authenticated client credential, never a request field (config-driven via
  `MCP_CLIENT_REGISTRY`; identifiers + policy only, any secret referenced by env *name*). Client
  **scopes** (`context.read`, `graph.read`, `ledger.read`, `context.verify`) gate the tool surface
  **additively** on top of the user team ACLs. The verifier binds the validated `client_id` into the
  signed receipt, so a receipt for client A does not validate for client B.
- `platform_trust.py` (PR-32) — `context.platform_trust` is the official-client gate: a
  `verification_required` client is platform-trusted **only** with a valid, client-matched, passing
  receipt; otherwise a structured denial reason (`verification_required_no_receipt`,
  `receipt_unsigned`, `receipt_client_mismatch`, `receipt_signature_invalid`,
  `receipt_overall_not_passed`) — never a silent pass. A non-opted-in client gets `not_required`.

Tool schemas live in `mcp/tool_schemas/verification.py` (and the `trust_floor`/`trust_class`
additions in `graph.py`); the EntailmentClient backend lives in
`infrastructure/entailment/`.

## 12. Agent manifests + output schemas (`agents/` + `agent_output_schemas/`)

The "controlled specialists" layer — **twelve roles** since ADR-0030: `orchestrator`,
`implementation`, `test_layer`, `code_reviewer`, `delivery_planner`, `pr_planner`, `adr_writer`,
`infra_code`, and the four review-panel lenses `bug_reviewer`, `security_reviewer`,
`quality_reviewer`, `test_coverage_reviewer`. `agents/*.md` are the **product's** runtime
manifests (not Claude Code subagents): YAML frontmatter declares `allowed_tools`,
`max_context_calls` / `max_context_tokens` (must match `.claude/rules/token-budgets.md`),
`requires_evidence_ids`, `requires_human_approval` (true only on the orchestrator), and an
`output_schema` name; the body is the agent's instruction set.

The tool grants reflect ADR-0025: **every role gets `kb_search` plus native tools**
(`read_file`, `read_full`, `list_files`/`grep`; `edit_file` only where the role writes code). **No
manifest declares any `context.*` or `ledger.*` tool** — the orchestrator included; the governed
surface is for hosts/runtimes that need citation-grade flows, not a per-role grant. The KB budget
is the manifest's `max_context_calls`/`max_context_tokens` pair, enforced server-side in
`kb_search` itself. The four reviewer manifests do double duty: the review-panel service loads
their instruction bodies at runtime as its lens prompts (§16).

`services/mcp-server/src/agentic_mcp_server/agent_output_schemas/` holds the schemas those names
resolve against — `AGENT_OUTPUT_SCHEMAS` registers **seven**: `phased_pr_plan_v1`,
`implementation_plan_v1`, `test_plan_v1`, `review_findings_v1`, `delivery_plan_v1`, `pr_plan_v1`,
and `adr_draft_v1` (ADR-0030) — mirrored in `docs/contracts/agent-output-contracts.md`.
`review_findings_v1` is produced by five roles: `code_reviewer` and the four panel lenses. Two
enforcement layers, both structural rather than prompt-based:

- **Construction**: every claim-bearing component (`EvidencedClaim`, `ImplementationStep`,
  `PlannedTest`, `ReviewFinding`, `RolloutStep`, `PlannedPr`) requires non-empty `evidence_ids` —
  an unevidenced claim is *unconstructible*; what cannot be proven goes in `open_questions`.
- **Reference check**: `validate_evidence_references(output, known_evidence_ids)` walks the model
  tree (`referenced_evidence_ids`) and raises `AgentOutputValidationError` on any handle the
  retrieval never returned.

Models are frozen with `extra="forbid"` and pin `schema_version` (like the tool schemas).
Executable specs: `tests/contract/test_agent_output_schemas.py` (claims without evidence cannot
exist; unknown IDs fail) and `tests/contract/test_agent_manifests.py` (manifests stay consistent
with the schema registry and the budget rules).

## 13. Evaluation harness (`evals/`)

The benchmark layer (PR-12), contract in `docs/contracts/evals-report.md`. A **dev-only** uv
project (never deployed, never a service) with an editable path dependency on
`services/mcp-server`: the executor drives the broker in-process through `BrokerDeps` with a
`FakeSearchClient` against a migrated `TEST_DATABASE_URL` registry — the same seam mcp-server's
integration tests use, so every run exercises real budget, dedupe, rerank, and ledger behavior. It
never runs Alembic (kb-builder owns the schema) and never requires Azure.

- `retrieval_cases/` + `agent_task_cases/` — YAML cases covering all six §13 benchmark task
  types, plus the PR-38 alias golden set (`alias_golden_v1.yaml`, 25 hand-verified cases) and the
  PR-39 two-arm task-context set (`task_context_ab_v1.yaml`, ten realistic dev tasks). Each case
  seeds its own registry fixtures and search seeds; loader validators reject unknown fixture keys
  and unreachable search seeds.
- `harness/` — `executor.py` runs a case and normalizes `retrieval_event` rows into pure
  `RunRecord` dataclasses; `metrics.py` computes the eleven §13 metrics DB-free (build-plane
  metrics emit `not_measured` with null values, never faked); `baseline.py` diffs against the
  committed `baseline.json` (±5% relative ⇒ improved/regressed, else flat); `dashboard.py` is the
  ADR-0014 renderer behind `make dashboard` (dev-guide [06](06-observability.md)).
- `run.py` — the T1 entrypoint (`make eval-run`); writes `report.json` (gitignored), prints the
  table the `eval-runner` Claude Code subagent reads, `--update-baseline` reseeds the baseline.
- `run_all.py` — the **consolidated tiered runner** (`make eval-all`): T1 golden sets, T2 live-KB
  zero-LLM checks (alias accuracy + `get_task_context` latency), T3 the LLM-armed A/B smoke, T4
  the adversarial-fixture inventory, with T0 (`make verify`) opt-in via `--with-gates`.
  Unavailable tiers **skip with a stated reason**. The system view:
  `docs/architecture/evaluation-system.md`.
- Case success = expected-doc recall 1.0 **and** no ledger row with status `error`; broker
  denials are contractual outcomes, not failures.
- **Golden queries (PR-25, `docs/contracts/golden-query-evals.md`)** — a golden subset under
  `retrieval_cases/` carries `expected_evidence_ids`, ACL context, and an `intent`, scored by
  `harness/golden.py` for `evidence_recall`, `acl_leak_count`, per-`edge_type` precision/recall, and
  the PR-33 `intent_ordering_ok` metric. These are the authoritative **publish gates** for
  evidence-recall + ACL-leak that kb-builder cannot evaluate itself across the service boundary — the
  defence against *underlinking* (real citations that silently miss the one ADR/card that matters).

## 14. Security hardening (PR-13)

The runtime plane's trust boundary, contract in `docs/contracts/mcp-tools-contract.md`. Three
layers, all server-side — prompts enforce nothing:

- **`auth/rbac.py`** — `Requester` (subject + frozen team set, derived solely from the verified
  token's `groups`/`roles` claims via `teams_from_claims`; request-body fields can never name an
  identity) and `TeamAclAuthorization` (`team_acl_v1`): an artifact with empty `acl_teams`
  (migration `0008`, kb-builder) is org-public to any *authenticated* subject; non-empty requires
  a team intersection. `current_requester` **fails closed** — no session token is a `ToolError`,
  never a synthesized anonymous identity. Filtering applies at *every* surface: card retrieval,
  `read_pack` (re-filters the cached cards per reading requester and recomputes the summary),
  `request_more` reuse (reused ids are re-filtered; a fully-suppressed reuse falls through to a
  fresh filtered retrieval), `open_evidence` (re-hydrates from Postgres and re-filters — a pack
  handle is not a grant), `kb_search` and `get_task_context` (every hit/entity hydrated and
  filtered through the same shared query layer before ranking), and graph traversal (root node +
  each BFS hop filtered *before* expanding the frontier, so a restricted node is never returned
  nor transited through). Existence-oracle discipline: responses carry an `authorization` decision
  but **no filtered count**; an ACL-denied, missing, or never-in-pack `open_evidence` id all raise
  the identical "evidence not available" error; an unauthorized graph root returns the same empty
  result as an unknown id. The run budget requested by `create_pack` is clamped to a server-side
  maximum (18k) — the request value is not an escape hatch.
- **`context_broker/untrusted.py`** — deterministic regex injection scan (instruction overrides,
  role markers, chat-template tokens, secret-exfiltration asks, unicode direction/zero-width
  tricks) over card titles/summaries and expanded bodies. Advisory only: `injection_flagged` +
  `injection_signals` on the response, content returned **verbatim**, never rewritten, and never
  able to alter broker policy.
- **`telemetry/audit.py`** — `audit_context_access` emits one structured line per context
  expansion on the `agentic_mcp_server.audit` logger: subject + teams, tool, returned /
  ACL-suppressed / injection-flagged artifact ids. Ids and metadata only — never `body_text`;
  claim-derived values are sanitized so token contents cannot forge audit fields.

Secrets posture: the server's config remains identifiers only (`config.py` — DB URL, tenant,
audience; JWKS verification means no client secret exists), asserted by
`tests/unit/test_secret_surface.py`. Executable specs: `tests/integration/test_security.py`
(every ACL surface against real Postgres, audit suppression lines, verbatim injection e2e),
`tests/unit/test_rbac.py`, `test_untrusted.py`, `test_audit.py`.

## 15. Portable agent framework (`.copilot/` + `.opencode/`)

Host-native renderings of the canonical `agents/*.md` manifests (ADR-0009; contract:
`docs/contracts/portable-agent-framework.md`), so teams on GitHub Copilot or OpenCode adopt the
framework by copying a directory and setting one credential. Each host directory carries **13
agent files** (the twelve roles + `_template`) and the **two** framework skills
(`kb-first-file-fallback`, `evidence-citation` — ADR-0025 retired the old evidence-pack skills):

- `.copilot/` — `agents/*.agent.md` (`name`, `description`, `tools` in Copilot's MCP syntax:
  `context-broker/kb_search` plus the host's native aliases `read`/`search`/`edit` mapped from the
  canon's native tools), `skills/*.md` as host-neutral instruction modules, and
  `mcp/repository-settings.json` (`$COPILOT_MCP_CONTEXT_BROKER_TOKEN`,
  **`tools: ["get_task_context", "kb_search"]`**) + `mcp/vscode-mcp.json`
  (`${input:context-broker-token}`). See dev-guide
  [02](02-connect-your-editor.md) for using it.
- `.opencode/` — `agents/*.md` (OpenCode frontmatter: `description`, `mode` — orchestrator
  `primary`, specialists `subagent` — and a `tools` map enabling `context-broker_kb_search` plus
  the native tools), `skills/<name>/SKILL.md`, and `opencode.json` (remote MCP entry with
  `{env:CONTEXT_BROKER_TOKEN}` substitution).
- Each rendered body is the canonical instruction body **verbatim** (stamped
  `<!-- rendered from agents/<role>.md v<N> -->`) plus a generated "Framework guarantees
  (enforced server-side)" block. `_template` files carry the framework skeleton with an explicit
  description slot.
- **Composition is declared natively per host.** Copilot: the orchestrator's `agents: [...]` lists
  its **seven invocable specialists** (implementation, test_layer, code_reviewer,
  delivery_planner, pr_planner, adr_writer, infra_code — the four panel lenses are *not*
  orchestrator-invocable; they run in the review-panel service) plus matching `handoffs:` and the
  host `agent` tool, the one pinned exception to the tool-parity rule; specialists and the
  template carry `agents: []`. OpenCode: every agent's `permission` frontmatter denies `"*"` for
  `task` and `skill`, then allow-lists exactly its role — the orchestrator may launch the same
  seven and load the two skills; specialists launch nothing.

No generator in V1 — renderings are hand-authored and parity-pinned by
`services/mcp-server/tests/contract/test_portable_agent_exports.py`: exact tool parity per host
syntax (orchestrator-only tools never leak), budget numbers and framework rules present in every
body, host validity (OpenCode skill naming regex, Copilot 30k body cap), and a two-sided
secret scan (markers absent **and** every Authorization header value matches a
reference-by-name pattern). Enforcement remains server-side either way — a host format that
cannot express a budget loses only the documentation of the limit, never the limit itself.

The pinning model is **pinned minimum + whatever exists** (PR-20): the twelve roles and two skills
must always be present, and every manifest *discovered* in `agents/` — including agents an
adopting team adds — is held to the same checklist. `agents/check_parity.py` is the standalone
stdlib-only checker adopters run over copied trees; the contract suite smoke-tests it by
subprocess (exit 0 on this repo, exit 1 on seeded tool drift or a literal-looking credential).

## 16. Review-panel draft engine (`services/review-panel`, PR-40)

The third service (ADR-0030 §3 as amended by ADR-0031), summarized here for the tour — the
operations guide is [04 — Review drafts](04-review-drafts.md) and the contract is
`docs/contracts/review-panel.md`:

- **One bounded job per PR**: a LangGraph fan-out of the four reviewer lenses (each one LLM call
  against the corresponding `agents/*_reviewer.md` instruction body, loaded at runtime) →
  deterministic reconciliation (`review_panel/domain/reconcile.py`) → `code_reviewer` synthesis →
  `store_draft`. The graph has **no posting node**; the service holds **no GitHub write
  credential** (dev gate, asserted by `tests/contract/test_dev_gate.py`).
- **Storage**: only the dedicated `review_panel` schema — the checkpointer tables, `review_draft`
  (key `<repo>#<pr>@<head_sha>`, `ON CONFLICT DO NOTHING`), and its own `trace_span` — all
  bootstrapped idempotently (the documented Alembic exemption; rollback = drop the schema). It
  never touches a registry table and imports no kb-builder code.
- **Crash-resume**: the draft key doubles as the LangGraph `thread_id`; a killed run resumes
  without re-paying completed lens calls, and exactly one draft row lands.
- **Delivery**: `uv run review-panel draft <owner/repo> <pr>` (wrapped by
  `scripts/run_review_panel_local.sh`) — JSON on stdout, logs on stderr; a stored draft for the
  current head SHA is returned with zero model calls. Untrusted-content fencing wraps PR
  title/body/diff and any `kb_search` context (`REVIEW_PANEL_MCP_URL`, optional, fail-soft);
  schema-invalid lens output gets one bounded retry with the verbatim validator error fed back.

## 17. Tooling: the Obsidian vault export

`agentic_kb_builder/export_obsidian.py` renders the built KB as a browsable **Obsidian vault** —
each `knowledge_artifact` becomes one Markdown note, each `knowledge_edge` an Obsidian
`[[wikilink]]`, so the graph can be explored as linked notes instead of SQL:

```sh
cd services/kb-builder
export DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb   # migrated DB
uv run python -m agentic_kb_builder.export_obsidian --out ./vault [--kb-version X]
```

Without `--kb-version` it targets the active version. It is read-only over Postgres and deterministic
(stable slugs + ordering ⇒ byte-identical re-runs; the out dir is cleaned first).

## 18. What does not exist yet

Verified against the tree as of 2026-07-05 — recorded follow-ups, not forgotten work:

- **IaC** — `infra/` is a README describing the lean Azure footprint; no Bicep/Terraform.
- **A scheduled nightly build** — `.github/workflows/` contains only `ci.yml`. The build is run
  on demand via the CLI (or compose's opt-in `kb-build` profile); wiring the nightly schedule (and
  the compose nightly path) is a recorded follow-up.
- **Managed-identity auth for the production connectors** (ADR-0015 backlog) — only
  PAT-via-`token_env` is built. The GitHub git-tree-truncation complete walk is a tracked
  follow-up, as is Entra `groupMembershipClaims` configuration.
- **A run-owner/orchestrator ledger view** — `ledger.list_retrievals` is subject-scoped (each
  agent sees only its own events); the cross-subject view for a run's owner is a recorded
  follow-up.
- **An MCP fetch tool for review drafts** (the PR-41 candidate) — today the CLI is the fetch path
  (§16); the in-session `code_reviewer` reads drafts through it.
- **`get_task_context` in the role grants** — the tool is registered and shipped, but no agent
  manifest (and neither host rendering) grants it yet; wiring it into the roster is an open
  decision, not an accident.
- **Graph-corroborated `deterministic`-tier `kb_search` hits** — the `confidence_tier` field is
  the declared extension point, but every keyword-ranked hit is `interpreted` today.
- **A true per-task `kb_search` budget boundary** — the window is per (MCP session, subject), so
  "per task" is enforceable only as "per session"; a host-signaled task boundary or TTL is
  deliberately deferred until real usage data exists (2026-07-03 architecture review).

## 19. Reading order for a new dev

1. `docs/architecture/00-overview.md` (15 min) — the blueprint.
2. [20 — Architecture for contributors](20-architecture-for-contributors.md) — the invariants and why.
3. `services/kb-builder/src/agentic_kb_builder/domain/` — the vocabulary.
4. `services/kb-builder/src/agentic_kb_builder/application/build_runner.py` top-to-bottom — the
   spine everything hangs on.
5. One enrichment layer end-to-end (suggest docify: extract_fn → docify_backend → write → its tests).
6. `services/kb-builder/tests/integration/test_build_engine.py` — the executable spec for the
   engine's guarantees.
7. The runtime side: `context_broker/kb_search.py` and `task_context.py` (the preferred surface),
   then `pack.py`/`retrieval.py` (the governed machinery) with
   `tests/integration/test_context_broker.py` as the spec.
