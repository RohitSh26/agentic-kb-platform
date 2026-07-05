# Architecture Overview — Agentic KB Platform (V1)

> Canonical, distilled reference. Source: `Agentic Knowledge-Based AI System — Architecture and
> Implementation Blueprint v0.1`, updated through ADR-0032 / PR-40 (all implemented). Agents,
> skills, and PR briefs reference this file by section name.

## 1. Thesis

An agentic system that helps developers plan and execute software work using a centrally managed
knowledge base. A human-approved **orchestrator** plans the work; specialized **subagents**
(twelve roles, ADR-0030) execute **KB-first** against a remote **MCP Context Broker**; a
**nightly-built** KB combines semantic knowledge (Docify) with code-structure knowledge (Graphify)
in one canonical **Postgres** registry, projected into **Azure AI Search** for retrieval (with a
local parity projection for dev — ADR-0017). Beside the serving path, a dev-gated **review draft
engine** precomputes four-lens PR review drafts that only a developer publishes (ADR-0031).

The design is deliberately lean: no event-driven cloud, no Redis, no API Management, no Blob (by
default), no graph database, no local SQLite in production (see ADR-0007), and no hosted tracing
SaaS — traces are Postgres rows behind a port (ADR-0032).

## 2. Two planes, three services

- **Runtime plane** (`services/mcp-server`) — serves agent requests through MCP. Developer ⇄ AI
  coding client ⇄ agents ⇄ MCP Context Broker ⇄ {Postgres truth, Azure AI Search projection}.
- **Build plane** (`services/kb-builder`) — refreshes the KB nightly and incrementally, and
  activates a new `kb_version` only after validation. Owns the Knowledge Registry schema and its
  Alembic migrations.
- **Review draft engine** (`services/review-panel`) — a third self-contained service, off the
  serving path: on demand, it fans out the four reviewer lenses over a pull request and persists
  **one draft** for the developer's in-session `code_reviewer` agent to pull (ADR-0031). It never
  posts to GitHub and owns only the dedicated `review_panel` Postgres schema
  (`docs/contracts/review-panel.md`).

> **Diagrams** (Mermaid, in this directory): [`e2e-flow-detailed.mmd`](e2e-flow-detailed.mmd) —
> the whole platform, build → serve → observe; [`seq-task-flow.mmd`](seq-task-flow.mmd) — one
> `get_task_context` call through its LangGraph backend; [`seq-review-flow.mmd`](seq-review-flow.mmd)
> — the dev-gated review-draft flow.

### End-to-end flow (at a glance)

The build plane runs nightly and incrementally; the runtime plane serves the last good `kb_version`.
KB-first/file-fallback (ADR-0025) and code-skeleton compression (ADR-0026) shape the runtime read path.

```
══════════════ BUILD PLANE (services/kb-builder, nightly, incremental) ══════════════

  0. LOCK      single-builder advisory lock — a second concurrent build aborts
     │            loudly (event=builder_lock_held) instead of interleaving
     ▼
  SOURCES  github_code · github_doc · azure_wiki · ado_card · git_metadata
     │  connectors (deterministic: same source state ⇒ same content_hash)
     ▼
  1. CONNECT   capture source_uri + source_version + content_hash
     │            hash unchanged?  ── yes ──▶ SKIP (no LLM, no embed)
     │ changed only
     ▼
  2. DOCIFY    summaries / concepts / source-backed facts (LLM)
     │            generation_cache hit ⇒ reuse, no LLM call
     ▼
  3. GRAPHIFY  code AST → files · symbols · endpoints · imports · calls · tests
     │            runs only for changed code files
     ▼
  4. LINK      structural edges (defined_in, imports) + semantic (embeddings);
     │            candidate → LLM judge → keep / drop; edges carry trust_class
     ▼
  5. EMBED + INDEX   embedding_cache gates every vector
     ▼
  POSTGRES (SOURCE OF TRUTH) ───────────▶ Azure AI Search (derived, rebuildable projection)
  source_item · knowledge_artifact ·       (never truth)
  knowledge_edge · *_cache · kb_build_run · retrieval_event · trace_span
     │  each source's knowledge commits as soon as it is ready (ADR-0029)
     ▼
  6. PUBLISH GATE   validate index/retrieval parity
                      fail  ──▶ keep serving last good kb_version
                      pass  ──▶ mark new kb_version ACTIVE (atomic)
     │  active kb_version
═════════════════ RUNTIME PLANE (services/mcp-server) ═══════════════════════════════
     ▼
  MCP CONTEXT BROKER   auth + ACL filter · kb_search · get_task_context ·
                       governed context.* / graph.* / ledger.* path
                       per-run & per-agent TOKEN BUDGETS · dedupe + rerank → 3–5 cards
                       writes a retrieval_event for every call · per-step trace_span rows (ADR-0032)
     │  ranked hits / task context (governed path: Evidence Pack, L0/L1 cards first)
     ▼
  AGENTS (orchestrator + subagents)   served in VS Code / Copilot / OpenCode
     triage → route → EXPLAIN or BUILD lane
     KB-FIRST, FILE-FALLBACK (ADR-0025):
        ① ask KB → enough?  ─ yes → answer / write code
                            └ no  → read the specific file (logged as a KB-gap signal)
     │  any code read (KB span OR fallback file)
     ▼
  COMPRESSION — code-skeleton (ADR-0026)   deterministic · reversible · ~41% fewer tokens
     read_file  ─▶ SKELETON (signatures + types + docstrings kept; bodies "… N lines elided")
     read_full  ─▶ exact original text (one call away; for verbatim quotes / exact edits)
     │  orient on skeletons, pull only the bodies it touches
     ▼
  VERIFY   context.verify_answer (governed path) — provenance check + receipt;
           every cited claim names its evidence IDs
```

Two distinct cost levers, kept separate: **broker budgets** (ADR-0025) bound *how much* an agent may
retrieve; **skeleton compression** (ADR-0026) bounds *how cheaply* each thing it reads is represented.

## 3. Developer experience

Developers install nothing about the KB. They need: an AI coding client, agent markdown files in the
repo, MCP config to the remote server, company SSO, and a local repo checkout. No local KB, vector
DB, Graphify, Search keys, or model keys on developer machines.

Flow: ask orchestrator → orchestrator drafts a plan (goal, subagents, context needed, retrieval
budget) → human approves/edits → orchestrator seeds a shared Evidence Pack → subagents get
role-specific views → subagents work **KB-first, file-fallback** (ADR-0025): ask the KB first, and
only read specific files directly when the KB is missing/partial/stale → orchestrator synthesizes a
phased PR plan with evidence IDs, risks, open questions.

The KB is a **budgeted helper, not a gate** (ADR-0025): specialists keep their native `read`/`grep`/
`glob` tools; `kb_search` carries a per-task token/call cap enforced in the tool, not the prompt, and
`get_task_context` answers a change task's scope, blast radius, and conventions in one call
(ADR-0030). Code the agent reads arrives **skeleton-first** (ADR-0026) — signatures and types kept,
bodies elided — with the exact body one `read_full` away. The full broker pipeline
(`context.create_pack → open_evidence → verify_answer`) remains available as the *governed* path,
but is no longer the only way to read code.

## 4. Knowledge Base design

The KB is a Postgres-backed Knowledge Registry with graph-shaped relationships and an Azure AI Search
projection — not "just a vector index."

### Artifact types
`source_item` (connectors), `concept` / `summary` / `source_backed_fact`
(Docify), `code_file` / `code_symbol` / `endpoint` / `test` (Graphify), `evidence_card` (MCP runtime).

### Edge types
`documents`, `implements`, `calls`, `imports`, `tests`, `requests`, `mentions`, `depends_on`,
`exposed_as`. Each edge carries `confidence`, `source` (graphify|linker|llm_judge|manual), `kb_version`.

> Graph decision: the graph abstraction is V1; the graph database is not. Edges live in Postgres;
> graph behavior is exposed through MCP tools.

## 5. Docify / Graphify / Linker

- **Docify** = semantic layer (ADR-0023). Inputs: docs, wiki, ADO cards. Document sources run
  through Graphify's LLM doc pipeline behind a thin `docify` adapter (same `LLM_*` config as every
  other model call). Outputs the same artifact shapes as before: concepts, summaries, source-backed
  facts. Trust is re-derived — a concept whose supporting sentence is a verbatim substring of the
  source becomes a citable `source_backed_fact`, otherwise an `interpreted` concept; the document
  node becomes an interpreted `summary`. Artifacts only (no concept→concept edges). Risk: generated
  summaries are interpreted knowledge — rank below current source-backed evidence.
- **Graphify** = code-structure layer. Inputs: code at commit SHA. The Graphify library runs once
  per repo (`graphify_tree`), resolving cross-file imports/calls/uses natively. Outputs: files,
  symbols, endpoints, imports, call edges, test links. Risk: it is a navigation aid; final evidence
  uses exact snippets at a source version.
- **Linker** connects Docify concepts to Graphify code via deterministic matching, source refs, path
  conventions, embedding similarity, and limited LLM help. Example:
  `Concept: User Embeddings → documents → Wiki; → requested_by → ADO card; → implemented_by →
  EmbeddingService.get_user_embedding; → exposed_as → GET /users/{userId}/embeddings; → tested_by →
  test_get_user_embedding_endpoint`.

## 6. Postgres Knowledge Registry (schema sketch)

```
source_item(source_id PK, source_type, source_uri, source_version, repo?, branch?, path?,
            external_id?, content_hash NOT NULL, last_seen_at, is_deleted=false, created_at, updated_at)

knowledge_artifact(artifact_id PK, artifact_type, source_id FK, title, body_text, content_hash,
                   artifact_hash, kb_version, knowledge_kind?, authority_score, freshness_score,
                   span_start?, span_end?, created_at, updated_at)
                   -- span_*: 1-based inclusive line span for code artifacts; the file path comes
                   -- from source_id -> source_item.path

knowledge_edge(edge_id PK, from_artifact_id FK, to_artifact_id FK, edge_type, confidence, source,
               kb_version, created_at)

generation_cache(cache_key PK, input_hash, prompt_version, model_name, model_params_hash,
                 output_schema_version, output_artifact_id FK, created_at)

generation_cache_artifact(cache_key FK, artifact_id FK, position, created_at,
                          PRIMARY KEY(cache_key, artifact_id))
                          -- source of truth for cache-hit output sets, ordered by position;
                          -- generation_cache.output_artifact_id is a denormalized copy of position 0

embedding_cache(artifact_id FK, text_hash, embedding_model, embedding_hash, azure_search_doc_id,
                created_at, PRIMARY KEY(artifact_id, text_hash, embedding_model))

kb_build_run(build_id PK, kb_version, status, started_at, completed_at, sources_seen,
             sources_changed, artifacts_created, artifacts_updated, artifacts_deleted, llm_calls,
             embedding_calls, search_docs_upserted, error_summary)

retrieval_event(retrieval_id PK, run_id, context_pack_id, agent_name, tool_name, status, query_text,
                normalized_query, retrieval_profile, kb_version, source_filters jsonb,
                returned_artifact_ids uuid[], reused_evidence_ids uuid[], new_evidence_ids uuid[],
                cache_hit bool, semantic_reuse bool, tokens_returned int, latency_ms int, created_at)
```

The sketch above is the core. The full current table set — code `search_text` enrichment (0016),
`retrieval_event.details` (0017), the durable model-output caches (0018), artifact centrality
(0019), the dashboard views `v_*` (0020), and `trace_span` (0021) — lives in
`docs/contracts/postgres-knowledge-registry.md`. Migration head: `0021_trace_span`.

### Raw document storage policy
Do not store full raw documents by default. Store pointers/versions/hashes/chunks/summaries/concepts.
- GitHub code/docs at SHA → no raw; store pointer + extracted artifacts.
- Azure Wiki versioned → usually no raw; mutable/no-version → maybe snapshot evidence-ready text.
- ADO cards → store normalized fields + revision (cards mutate).
- PDFs/images/attachments → not in V1 (Blob later).

## 7. Incremental nightly build

Rule: if `content_hash` and generation inputs are unchanged, do not call the LLM or re-embed.

1. Fetch source metadata + content (GitHub, Wiki, ADO). 2. Normalize. 3. Compute `content_hash`.
4. Compare to `source_item`. 5. If unchanged → skip everything. 6. If changed → update source_item,
Docify on generation_cache miss, Graphify for changed code, update artifacts+edges, embed on
embedding_cache miss, upsert changed docs to Search. 7. Validate retrieval/index consistency.
8. Mark new `kb_version` active only if validation succeeds.

### Cache keys
- Doc extraction (docify): `source_content_hash + doc_extract_prompt_version + model_name +
  model_params_hash + output_schema_version`.
- Concept rollup: `concept_id + sorted_supporting_artifact_hashes + rollup_prompt_version +
  model_name + output_schema_version`.
- Code graph: `repo + commit_sha + file_path + file_content_hash + graphify_version +
  parser_config_version`.

### Per-source persistence (ADR-0029) and crash durability (ADR-0027)
Each changed source's knowledge **commits the moment it is ready** (ADR-0029, superseding
ADR-0027's end-of-build atomic write): a mid-build failure loses only the in-flight source — it is
rolled back, counted, and retried next build with its `content_hash` unadvanced — while everything
already committed stays. Activation remains atomic and separate: the graph finalize and publish
gates flip the active `kb_version` all-or-nothing (ADR-0013). Beneath that, a side-committed,
content-keyed model-output cache (`doc_extraction_output`, `embedding_output` — ADR-0027) persists
the *raw* model outputs the moment they are produced, so a crashed-and-restarted build re-maps them
into a fresh `build_seq` with **zero model calls**. The durable cache is pure derived data, never
served.

### Single-builder advisory lock
A build takes a session-level `pg_try_advisory_lock` before doing anything; a second concurrent
build against the same database aborts immediately and loudly (`event=builder_lock_held`) rather
than interleaving with the first
(`services/kb-builder/src/agentic_kb_builder/application/builder_lock.py`).

## 8. MCP Context Broker

The broker is the policy/retrieval/dedupe/evidence/budget layer — not a thin wrapper over search.
It registers **twelve tools** (`MCP_SCHEMA_VERSION = "1.10.0"`; the versioned request/response
schemas and full semantics live in `docs/contracts/mcp-tools-contract.md`).

The preferred first stops (ADR-0025, ADR-0030):

| Tool | Purpose |
|---|---|
| `kb_search` | KB-first retrieval: one budgeted, ACL-filtered ranked search over the active KB. A bare `{"query": ...}` is the entire request; identity and budget bind to the authenticated session. |
| `get_task_context` | One-call task context: resolved scope, blast radius (callers/callees/tests), conventions, and similar prior changes — confidence-tiered, cited, budget-capped. Zero LLM at query time (a LangGraph graph of parallel pure-retrieval nodes). |

The **governed path** — registered and optional; the route when citation-grade provenance is
required:

| Tool | Purpose |
|---|---|
| `context.create_pack` | Build the run's Evidence Pack from an approved context plan |
| `context.read_pack` | Role-specific view of an existing pack |
| `context.request_more` | Justified incremental retrieval (reuse-first; a bare query is rejected) |
| `context.open_evidence` | Expand one evidence card to L2/L3 raw text, by handle |
| `context.expand` | Trust-tiered BFS expansion from seed artifact ids; returns evidence cards |
| `context.verify_answer` | Provenance verifier (levels L0–L3); returns a verification receipt |
| `context.platform_trust` | Official-client gate: is the client's answer platform-trusted? |
| `context.create_change_pack` | BUILD-lane selector: the small file set (target/test/dependency) to edit for a code-change task |
| `graph.get_neighbors` | Graph traversal over `knowledge_edge` (depth 1–3, trust-aware) |
| `ledger.list_retrievals` | Retrieval ledger for a run (subject-scoped) |

There is **no unrestricted** KB search tool. `kb_search` is deliberately simple to *call* but not
unrestricted: the server enforces a dual hard cap (call count AND cumulative tokens) per
(MCP session, authenticated subject), filters every hit through the same team ACL as every other
tool, and writes a `retrieval_event` row per call. When the budget closes, the tool answers with a
notice and empty results — never a tool error — so the agent keeps working with files instead of
crashing (ADR-0025 §4). Every tool call is ledgered, ACL-filtered before returning, and budget
decisions are made server-side — prompts enforce nothing.

## 9. Evidence Packs

A run-scoped shared context object all subagents use, so they don't each form a different worldview —
the unit of the governed path. Sections: known_facts, constraints, relevant_concepts,
relevant_code_symbols, relevant_tests, open_questions. Each evidence card has id, type, title,
summary, confidence, authority_score, `tokens_if_expanded`.

### Evidence card levels
- L0: title/type/path/score/token-cost → orchestrator discovery.
- L1: tiny summary + evidence id + source metadata → most subagents most of the time.
- L2: selected excerpt or symbol body → impl/test/review agents when needed.
- L3: full source chunk/file excerpt → rare, exact detail only.
- L4: cross-source synthesized section → orchestrator final synthesis.

## 10. Token-saving controls

Two independent levers (see ADR-0025 and ADR-0026):

- **Budget — bound *how much* an agent retrieves (in code, not the prompt).** `kb_search` carries a
  per-task call + token cap; when the cap is hit the tool stops answering and tells the agent to work
  with what it has or read the specific files it still needs. The broker also enforces per-run and
  per-agent budgets, dedupe, and a 3–5 card cap. Supporting mechanisms: shared Evidence Pack · evidence
  cards first · exact + semantic query cache · role-specific views · evidence IDs/handles · precomputed
  summaries · AST/symbol extraction. Budgets: see `.claude/rules/token-budgets.md`.
- **Compression — bound *how cheaply* each read is represented (ADR-0026).** Code the agent reads is
  returned **skeleton-first**: a deterministic, reversible compressor keeps imports, signatures, type
  hints, and the docstring, and elides function bodies (`… # N lines elided`) — ~41% fewer tokens
  overall, 60–80% on large files. The exact original is one `read_full` away. Skeletons are for
  **thinking, never citing**; verbatim quotes always read the reversible original.

Token policy (ADR-0025): the KB is **preferred-first, not mandatory**. An agent asks the KB first and
does not re-read what the KB already supplied; it reads specific files directly only when the KB is
missing, partial, or stale, or exact current code is needed. The cap — not the prompt — guarantees the
agent stays efficient. (The older "may not think by retrieving / a bare `{\"query\": \"…\"}` is
rejected" rule still governs the *broker* `context.request_more` path, which requires a justified
question, but it is no longer the only way to read code.)

## 11. Agent design (product runtime)

Markdown agent files behave like manifests, not just prompts; server-side policy still enforces the
real limits even if a prompt fails. **Twelve roles** (ADR-0030), canonical in `agents/` with strict
output schemas and evidence rules, rendered host-natively into `.copilot/` / `.opencode/` and held
at parity (`python3 agents/check_parity.py`):

- **The original six**: orchestrator, implementation, test_layer, code_reviewer, delivery_planner,
  pr_planner.
- **Added by ADR-0030**: adr_writer and infra_code (BUILD-lane specialists the orchestrator can
  reach), plus the four-lens review panel — bug_reviewer, security_reviewer, quality_reviewer,
  test_coverage_reviewer.

**`code_reviewer` was redefined by ADR-0031**: it is the in-session reviewer/presenter that works
*with* the developer — it pulls the panel's stored draft when one exists (else reviews directly
through the four lenses), reconciles, presents in the team's format, revises on feedback, and
publishes only on the developer's ask, under the developer's own authorization. The four panel
lenses run server-side in the **review draft engine** (`services/review-panel`): an on-demand
LangGraph fan-out → deterministic reconcile → one persisted draft
(`scripts/run_review_panel_local.sh` / `uv run review-panel draft`). The panel never posts to
GitHub and holds no GitHub write credential; the framework orchestrator never launches the lenses
in-session. Contract: `docs/contracts/review-panel.md`.

**KB-first, file-fallback (ADR-0025).** Specialists keep their native `read`/`grep`/`glob` tools (and
`edit` for implementers); the KB is an **optional, budgeted tool**, never a gate that removes the
model's hands. Each manifest expresses the preference: ask the KB first (`kb_search` /
`get_task_context`), use and cite it when it suffices, and read specific files directly only when the
KB is missing/partial/stale or exact current code is needed. The single enforced restriction is the
`kb_search` budget (in the tool, not the prompt). A **file-fallback is logged as a KB-gap signal** —
a precise pointer to where the KB should improve. Code reads are **skeleton-first** (ADR-0026), with
`read_full` for exact bodies. Server-side budgets, ACL filtering, and the retrieval ledger remain the
backstop; the broker's governed `create_pack → open_evidence → verify_answer` path stays available for
when citation-grade provenance is required.

## 12. Security & access control

Built into the MCP boundary. Authenticate via Entra ID. Use managed identity for MCP → Postgres /
Search / model; Key Vault only for the rest. Attach ACL metadata to sources/artifacts; filter
retrieval by requester authorization before returning. Treat retrieved docs as untrusted — they
cannot override system/tool instructions. Detect and mark prompt-injection-like content. Log all
context expansions and source access.

## 13. Observability & evaluation

Everything is read-only over Postgres: the system answers "what happened and what did it cost"
from its own records, with no external telemetry vendor.

- **Retrieval ledger** (`retrieval_event`) — one row per tool call, complete by construction:
  crashes still write `error` rows and refund any budget charged before the failure.
- **Dashboard** (ADR-0014, `docs/contracts/observability-dashboard.md`) — `make dashboard` renders
  a static HTML + Markdown operator view from the `v_*` views over the ledger and `kb_build_run`.
- **Per-step tracing** (ADR-0032, `docs/contracts/tracing.md`) — both owned LangGraph graphs (the
  `get_task_context` backend and the review-panel draft engine) emit spans through a `TraceSink`
  port to Postgres `trace_span` tables (`TRACE_SINK=postgres|none`, default `postgres`). Fail-soft
  always: a dead sink drops the span, never the call, and never charges budget. Spans carry
  aggregate metadata only — never prompts, bodies, or secrets. No hosted tracing SaaS; the earlier
  LangSmith commitment was withdrawn before activation, and Langfuse later is one adapter away.

### Core metrics
`context_tokens_per_successful_task`, `duplicate_context_tokens`, `evidence_reuse_rate`,
`retrieval_calls_per_agent`, `semantic_cache_hit_rate`, `llm_calls_per_build`,
`embedding_calls_per_build`, `unsupported_claim_rate`, `human_plan_edit_rate`,
`missing_context_rate`, `active_kb_age`.

### Evaluation system
The benchmark exists — build on it before expanding autonomy. Golden-query retrieval cases and
agent task cases live under `evals/` (each case lists expected docs, files, symbols, tests, and
open questions); `make eval-all` runs the consolidated T0–T4 report (`evals/run_all.py`). Design
and tier map: `docs/architecture/evaluation-system.md`; report/baseline schema:
`docs/contracts/evals-report.md`.

## 14. Risks (top)

Subagents over-retrieve (→ budgets, shared pack, justification, dedupe) · generated summaries treated
as truth (→ rank current source above summaries) · Search index drift (→ derived projection, validate
after build, rebuild from Postgres) · ADO mutation (→ normalized snapshots) · noisy graph edges (→
confidence/source/version, evaluate top expansions) · prompt injection (→ untrusted wrapping,
server-side policy) · over-engineering cloud (→ keep V1 lean) · graph DB too early (→ Postgres edges,
migrate behind MCP).

## 15. Future upgrade path

Add behind the existing MCP interface only when triggered: Blob (raw archives/large bodies), graph DB
(deep traversal/impact analysis), Redis (QPS/hot-cache pressure), API Management (multi-team
governance), event-driven ingestion (nightly not fresh enough). See ADR-0007 for triggers.
