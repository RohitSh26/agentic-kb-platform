# 02 — Implementation tour (PR-01 → PR-33)

> A guided walk through the code as it exists today. Read
> [01 — Design deep dive](01-design-deep-dive.md) first for the *why*; this document is the *how*
> and *where*. Paths are repo-relative; line numbers drift, so prefer the named symbols.

## Layout at a glance

```
services/kb-builder  the nightly build: connectors → build engine → docify/graphify → linker →
                     indexing. Owns the registry: SQLAlchemy models + Alembic migrations.
services/mcp-server  the runtime plane: auth, telemetry, tool contracts, health, Context Broker
docs/contracts/      markdown cross-service contracts — the only thing the services share
agents/              product runtime agent manifests (served later by MCP; not Claude Code agents)
evals/               dev-only uv project: benchmark cases + harness + baseline (PR-12)
infra/               README describing the lean Azure footprint; no IaC yet
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

- `tool_schemas/` — the Context Broker tool contracts (PR-09), mirrored in
  `docs/contracts/mcp-tools-contract.md` (`MCP_SCHEMA_VERSION` **1.7.0** at PR-32). `base.py` holds
  `McpModel` (frozen, `extra="forbid"`, pinned `schema_version`); `context.py` / `graph.py` /
  `ledger.py` define request+response pairs for the broker tools; `verification.py` adds
  `context.verify_answer` (the verifier ladder L0→L3) and `context.platform_trust` (the
  official-client gate); `graph.py` carries the `trust_floor` / `include_inferred` request fields and
  the `GraphNeighbor.trust_class` / `claim_supporting` response fields (PR-23); `context.create_pack`
  takes an optional `intent` (PR-33). `evidence.py` defines `EvidenceCard` (L0/L1 handle: id, type,
  title, summary, confidence, authority, `tokens_if_expanded`, plus the PR-33 temporal fields
  `source_kind` / `temporal_state`) and `AgentRole`. Policy is encoded in the schema itself:
  `RequestMoreRequest` requires question/why_needed/decision_needed/already_checked/max_tokens (a bare
  `{"query": ...}` fails validation before any broker code runs), denied responses must carry
  `denial_reason`, `OpenEvidenceResponse` names its payload `untrusted_content`, and a
  `verify_answer` request with no claims (or any claim with empty `evidence_ids`) fails at the schema
  boundary. `tool_registry.py` exposes `TOOL_SCHEMAS`, the authoritative tool-name → schema table the
  server registers from.
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
| `knowledge_edge` | The graph | `uq_knowledge_edge_linker` partial unique (from, to, edge_type) WHERE source='linker'; a second partial unique WHERE source='llm_judge' (PR-29); `trust_class` CHECK in the bucket set (PR-23); `relation_schema_version` + `evidence` (PR-28); `valid_from_seq` / `invalidated_at_seq` (PR-27) |
| `generation_cache` | LLM call gate | PK = deterministic cache_key |
| `generation_cache_artifact` | Ordered cache→artifact mapping | **The** source of truth for cache-hit output sets; `output_artifact_id` on the parent is a denormalized copy of position 0 |
| `embedding_cache` | Embedding call gate + canonical vector store | PK (artifact_id, text_hash, embedding_model); `embedding` holds the vector so the index rebuilds without re-embedding; `azure_search_doc_id` stamped on upsert |
| `kb_build_run` | Build audit + version lifecycle | `uq_kb_build_run_single_active` partial unique on status='active'; `build_seq` BIGINT UNIQUE from the `kb_build_seq` sequence (PR-27); publish-gate result columns + `allow_large_delta` (PR-25) |
| `retrieval_event` | Runtime ledger (used from PR-10) | indexes on run_id, normalized_query, kb_version |
| `relationship_candidate` | Phase-3A audit artifact (PR-28) | cross-domain candidate pairs + firing `signals` (jsonb); **never served through MCP**, no membership columns |
| `relationship_judgment_cache` | Phase-3B LLM-judge gate (PR-29) | PK on sorted endpoint content hashes + schema/prompt/model versions; a hit ⇒ zero LLM calls |
| `entailment_cache` | L3 verifier gate (PR-31) | keyed on `(claim_hash, evidence_ids_hash, prompt_version, model_version)`; kb-builder owns it, mcp-server reads/writes via raw SQL |

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
`relationship_judgment_cache` (PR-29); `0015` `entailment_cache` (PR-31). Latest revision is **0015**.

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
  default factory.
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
  configured-but-unset is a hard error; the value lives only in a local handed to the backend
  factory), `FilteredFetchBackend` (wraps any backend: excluded paths are never fetched, hashed,
  or stored; stamps the source's `acl_teams` onto surviving refs), and
  `connectors_from_config(config, backend_factory)` — the seam where the real API backends will
  plug in. `acl_teams` flows config → `SourceRef` → `source_item` on insert and update; note it
  has no runtime enforcement effect until artifact-level ACL propagation lands (the mcp-server
  filters on `knowledge_artifact.acl_teams`, which stays org-public for now).

## 5. Build engine + the `build` CLI (`services/kb-builder/src/agentic_kb_builder/application/` + `build.py`)

The heart of the platform. The product-facing entry point is `agentic_kb_builder/build.py`
(ADR-0010) — `python -m agentic_kb_builder.build`. It wires connectors → extractors → linker →
embed → index → validate → activate exactly as `BuildRunner` orchestrates; adopters never call the
sub-steps. `default_collaborators` are no-cloud: `DocExtractor.from_env()` (docify — Graphify's LLM
doc pipeline, local Ollama by default), the whole-tree Graphify extractor, a `LocalHashEmbedder`, and the
in-memory `FakeSearchClient` projection. Flags: `--backend {local,production}` (default `local`;
`production` selects the GitHub/ADO factory of §4), `--no-activate`, `--no-git-metadata`,
`--allow-large-delta`, `--kb-version`, `--version`. The `git_metadata` connector is appended **last**
so its commit artifacts can resolve changed-file → code edges against code produced earlier in the
same build.

The application package itself:

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
   upsert `source_item` (on the natural-identity constraint) → `_docify_gated` → `_graphify_gated`
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
  (ids assigned) but never commits; the runner owns the transaction and records the cache row
  *after* a successful write, so a failed write cannot leave a cache entry pointing at nothing.

The model call is generation-cache gated (`doc_extract_cache_key`, keyed on
`DOC_EXTRACT_PROMPT_VERSION`), so an unchanged document makes no model call.

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
  keyword-searchable `search_text` (ADR-0018). Uses `str.split("\n")`, never `splitlines()`.
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
  place the model rules on a relationship, over *only* the bounded candidate set. For each pair it
  asks the `ModelClient` for a verdict under the closed ontology (V1 vocabulary: `documents`) + trust
  buckets, and the verdict becomes an edge — `INFERRED_HIGH`/`INFERRED_LOW`/`AMBIGUOUS` are written
  (`source='llm_judge'`, with `valid_from_seq` so the broker serves them as routing hints),
  `REJECTED` is cache-only. A `supporting_quote` that is not a verbatim substring of a source span is
  downgraded to `AMBIGUOUS` (invariant 7); the judge may **never** emit `EXTRACTED`. Every call is
  gated by `relationship_judgment_cache` (sorted endpoint hashes + schema/prompt/model versions) — a
  hit makes zero LLM calls; judge edges upsert on the `source='llm_judge'` partial unique index.

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

The policy layer behind the broker tools (PR-10; the surface grew to **eight** tools through PR-33 —
the six retrieval/graph/ledger tools plus `context.verify_answer` and `context.platform_trust`,
`docs/contracts/mcp-tools-contract.md`, `MCP_SCHEMA_VERSION` 1.7.0). Identity is always the
authenticated session subject — `agent_name`/`role` request fields are correlation/view data only.
Since PR-18 the `role` field is free-form (charset-guarded like `run_id`, since it lands in audit
logs): a team-defined `security_auditor` reads the shared pack exactly like a canonical role,
because the broker never branches on the value. **Retrieval, graph, and provenance now filter by
interval membership** (the `valid_from_seq <= S AND (invalidated_at_seq IS NULL OR
invalidated_at_seq > S)` predicate of `version-membership.md`), not `kb_version` label-equality: the
broker resolves the active build's `build_seq` once and serves every row that is a member of it.

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
  record. Since PR-19, the per-subject allowance map is deployment config: the optional
  `MCP_AGENT_ALLOWANCES` env var (JSON `{subject: {max_requests, max_tokens}}`) is parsed
  fail-fast by `parse_agent_allowances` at boot — adopting teams grant their own agents their
  own allowances without touching server code; unlisted subjects keep the conservative default.
- `dedupe.py` — deterministic normalized-token similarity (no embeddings in the broker, V1).
- `graph.py` + `trust.py` (PR-23) — depth/fan-out-capped **trust-aware** BFS over `knowledge_edge`;
  titles + edge metadata only. `trust_floor` (default `EXTRACTED`) and `include_inferred`
  (default `false`) gate which buckets are returned; every `GraphNeighbor` carries the edge's
  `trust_class` and a `claim_supporting` flag (true only for `EXTRACTED`). `AMBIGUOUS`/`REJECTED`
  are never returned or transited; trust filtering composes with the per-hop ACL filter.
- `temporal.py` (PR-33) — deterministic, zero-LLM `source_kind` + `temporal_state` derivation and
  the transparent, logged intent-aware re-weighting (driven by `create_pack.intent`). It is a
  ranking/label signal only — independent of the L0 `not_stale` check, never removes historical
  evidence, never promotes a contradicting doc into claim support.
- `ledger.py` + `error_ledger.py` + `audit.py` — every call writes a `retrieval_event` row,
  including failures (ledger-only status `error`, `"-"` sentinels for unresolved run/kb_version);
  `ledger.list_retrievals` audits itself.
- `authorization.py` — the ACL/trust decision objects threaded through every surface.
- `infrastructure/search/` + `infrastructure/postgres/keyword_search.py` — `SearchClient` is the
  seam: a Postgres keyword scorer locally, Azure AI Search later behind the same interface.

The executable spec is `tests/integration/test_context_broker.py`: exact + semantic reuse,
per-agent and per-run denial, evidence expansion/truncation, budget-race concurrency, the 5-card
cap, and an injection-style document that must come back verbatim as data without changing any
broker decision.

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

## 13. Evaluation harness (`evals/`)

The benchmark layer (PR-12), contract in `docs/contracts/evals-report.md`. A **dev-only** uv
project (never deployed, never a service) with an editable path dependency on
`services/mcp-server`: the executor drives `create_pack` / `request_more` / `open_evidence`
in-process through `BrokerDeps` with a `FakeSearchClient` against a migrated `TEST_DATABASE_URL`
registry — the same seam mcp-server's integration tests use, so every run exercises real budget,
dedupe, rerank, and ledger behavior. It never runs Alembic (kb-builder owns the schema) and never
requires Azure.

- `retrieval_cases/` + `agent_task_cases/` — YAML cases covering all six §13 benchmark task
  types. Each case seeds its own registry fixtures and search seeds, optionally scripts broker
  calls per agent (exercising exact reuse, semantic reuse, L2 expansion, conflicting evidence),
  and lists expected docs/files/symbols/tests/open questions. Loader validators reject unknown
  fixture keys and search seeds no scripted query can ever reach.
- `harness/` — `executor.py` runs a case and normalizes `retrieval_event` rows into pure
  `RunRecord` dataclasses; `metrics.py` computes the eleven §13 metrics DB-free (build-plane
  metrics emit `not_measured` with null values, never faked; `unsupported_claim_rate` is
  `measured_scripted` via the PR-11 `validate_evidence_references` seam); `baseline.py` diffs
  against the committed `baseline.json` (±5% relative ⇒ improved/regressed, else flat).
- `run.py` — entrypoint (`make eval-run`); writes `report.json` (gitignored), prints the table
  the `eval-runner` Claude Code subagent reads, `--update-baseline` reseeds the baseline.
- Case success = expected-doc recall 1.0 **and** no ledger row with status `error`; broker
  denials are contractual outcomes, not failures.
- **Golden queries (PR-25, `docs/contracts/golden-query-evals.md`)** — a golden subset under
  `retrieval_cases/` carries `expected_evidence_ids`, ACL context, and an `intent`, scored by
  `harness/golden.py` for `evidence_recall`, `acl_leak_count`, per-`edge_type` precision/recall, and
  the PR-33 `intent_ordering_ok` metric. These are the authoritative **publish gates** for
  evidence-recall + ACL-leak that kb-builder cannot evaluate itself across the service boundary — the
  defence against *underlinking* (real citations that silently miss the one ADR/card that matters).

## 14. Security hardening (PR-13)

The runtime plane's trust boundary, contract in `docs/contracts/mcp-tools-contract.md`
(`MCP_SCHEMA_VERSION` 1.2.0). Three layers, all server-side — prompts enforce nothing:

- **`auth/rbac.py`** — `Requester` (subject + frozen team set, derived solely from the verified
  token's `groups`/`roles` claims via `teams_from_claims`; request-body fields can never name an
  identity) and `TeamAclAuthorization` (`team_acl_v1`): an artifact with empty `acl_teams`
  (migration `0008`, kb-builder) is org-public to any *authenticated* subject; non-empty requires
  a team intersection. `current_requester` **fails closed** — no session token is a `ToolError`,
  never a synthesized anonymous identity. Filtering applies at *every* surface: card retrieval,
  `read_pack` (re-filters the cached cards per reading requester and recomputes the summary),
  `request_more` reuse (reused ids are re-filtered; a fully-suppressed reuse falls through to a
  fresh filtered retrieval), `open_evidence` (re-hydrates from Postgres and re-filters — a pack
  handle is not a grant), and graph traversal (root node + each BFS hop filtered *before*
  expanding the frontier, so a restricted node is never returned nor transited through).
  Existence-oracle discipline: responses carry an `authorization` decision but **no filtered
  count**; an ACL-denied, missing, or never-in-pack `open_evidence` id all raise the identical
  "evidence not available" error; an unauthorized graph root returns the same empty result as an
  unknown id. The run budget requested by `create_pack` is clamped to a server-side maximum
  (18k) — the request value is not an escape hatch.
- **`domain/untrusted.py`** — deterministic regex injection scan (instruction overrides, role
  markers, chat-template tokens, secret-exfiltration asks, unicode direction/zero-width tricks)
  over card titles/summaries and expanded bodies. Advisory only: `injection_flagged` +
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

## 15. Portable agent framework (`.copilot/` + `.opencode/`, PR-15)

Host-native renderings of the canonical `agents/*.md` manifests (ADR-0009; contract:
`docs/contracts/portable-agent-framework.md`), so teams on GitHub Copilot or OpenCode adopt the
framework by copying a directory and setting one credential:

- `.opencode/` — `agents/*.md` (OpenCode frontmatter: `description`, `mode` — orchestrator is
  `primary`, specialists `subagent` — and a `tools` glob map enabling exactly the canonical
  `allowed_tools` as `context-broker_<tool>` entries), `skills/<name>/SKILL.md` (the framework
  procedures: evidence-pack-orchestration, context-request-discipline, evidence-citation), and
  `opencode.json` (remote MCP entry for the broker with `{env:CONTEXT_BROKER_TOKEN}`
  substitution; `context-broker_*` disabled globally, re-enabled per agent).
- `.copilot/` — `agents/*.agent.md` (`name`, `description`, `tools:
  ['context-broker/<tool>', …]`), the same three skills as host-neutral instruction modules,
  and `mcp/repository-settings.json` (`$COPILOT_MCP_CONTEXT_BROKER_TOKEN`) +
  `mcp/vscode-mcp.json` (`${input:context-broker-token}`).
- Each rendered body is the canonical instruction body **verbatim** plus a generated
  "Framework guarantees (enforced server-side)" block (budgets, `requires_evidence_ids`,
  `output_schema`, request-more discipline, untrusted-content rule). `_template` files carry the
  framework skeleton with an explicit description slot.
- **Composition is declared natively per host (PR-16).** Copilot: the orchestrator's
  `agents: [...]` lists the five canonical specialist names (plus `handoffs:`, VS Code-only, and
  the host `agent` tool — the single pinned exception to broker-only tool lists); specialists
  and the template carry `agents: []`. OpenCode: every agent's `permission` frontmatter denies
  `"*"` for `task` and `skill`, then allow-lists exactly its role — the orchestrator may launch
  the five specialists and load evidence-pack-orchestration + evidence-citation; specialists
  launch nothing and load context-request-discipline + evidence-citation (tracking the canonical
  `context.request_more` grant). See the contract's Composition section.

No generator in V1 — renderings are hand-authored and parity-pinned by
`services/mcp-server/tests/contract/test_portable_agent_exports.py`: exact tool parity per host
syntax (orchestrator-only tools never leak), budget numbers and framework rules present in every
body, host validity (OpenCode skill naming regex, Copilot 30k body cap), and a two-sided
secret scan (markers absent **and** every Authorization header value matches a
reference-by-name pattern). Enforcement remains server-side either way — a host format that
cannot express a budget loses only the documentation of the limit, never the limit itself.

Since PR-20 the pinning model is **pinned minimum + whatever exists**: the six roles and three
skills must always be present, and every manifest *discovered* in `agents/` — including agents
an adopting team adds — is held to the same checklist (composition is structural: the
`context.create_pack` grant decides who may launch subagents and be `mode: primary`).
`agents/check_parity.py` is the standalone stdlib-only checker adopters run over copied trees;
the contract suite smoke-tests it by subprocess (exit 0 on this repo, exit 1 on seeded tool
drift or a literal-looking credential) and proves a parity-clean seventh agent passes.

## 16. Tooling: the Obsidian vault export

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

## 17. What does not exist yet

- The **orchestrator runtime** that executes the agent manifests, and **IaC** (`infra/` is a README).
- **Managed-identity auth** for the production connectors is backlog (ADR-0015) — only PAT-via-
  `token_env` is built. The GitHub git-tree-truncation complete walk is a tracked follow-up.
- Recorded follow-ups: Entra `groupMembershipClaims` configuration, and **run-scoped ledger
  authorization** (V1 ledger records are visible to any authenticated subject that knows the
  `run_id`). (Connector backends and artifact-level ACL propagation — earlier listed here — now
  exist: production GitHub/ADO backends via ADR-0015, and ACL propagation onto live artifacts via the
  PR-27 invalidation pass.)

## 18. Reading order for a new dev

1. `docs/architecture/00-overview.md` (15 min) — the blueprint.
2. This guide's doc 01 — the invariants and why.
3. `services/kb-builder/src/agentic_kb_builder/domain/` — the vocabulary.
4. `services/kb-builder/src/agentic_kb_builder/application/build_runner.py` top-to-bottom — the
   spine everything hangs on.
5. One enrichment layer end-to-end (suggest docify: extract_fn → docify_backend → write → its tests).
6. `services/kb-builder/tests/integration/test_build_engine.py` — the executable spec for the
   engine's guarantees.
