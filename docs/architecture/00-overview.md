# Architecture Overview — Agentic KB Platform (V1)

> Canonical, distilled reference. Source: `Agentic Knowledge-Based AI System — Architecture and
> Implementation Blueprint v0.1`. Agents, skills, and PR briefs reference this file by section name.

## 1. Thesis

An agentic system that helps developers plan and execute software work using a centrally managed
knowledge base. A human-approved **orchestrator** plans the work; specialized **subagents** execute
against one shared **Evidence Pack** governed by a remote **MCP Context Broker**; a **nightly-built**
KB combines semantic knowledge (Docify) with code-structure knowledge (Graphify) in one canonical
**Postgres** registry, projected into **Azure AI Search** for retrieval.

The design is deliberately lean: no event-driven cloud, no Redis, no API Management, no Blob (by
default), no graph database, no local SQLite in production. Those are deferred until real pressure
justifies them (see ADR-0007).

## 2. Two planes

- **Runtime plane** — serves agent requests through MCP. Developer ⇄ AI coding client ⇄ orchestrator
  ⇄ MCP Context Broker ⇄ {Postgres truth, Azure AI Search projection, model endpoint}.
- **Build plane** — refreshes the KB nightly and activates a new `kb_version` only after validation.

### End-to-end flow (at a glance)

The build plane runs nightly and incrementally; the runtime plane serves the last good `kb_version`.
KB-first/file-fallback (ADR-0025) and code-skeleton compression (ADR-0026) shape the runtime read path.

```
══════════════ BUILD PLANE (services/kb-builder, nightly, incremental) ══════════════

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
  knowledge_edge · *_cache · kb_build_run · retrieval_event
     │
     ▼
  6. PUBLISH GATE   validate index/retrieval parity
                      fail  ──▶ keep serving last good kb_version
                      pass  ──▶ mark new kb_version ACTIVE
     │  active kb_version
═════════════════ RUNTIME PLANE (services/mcp-server) ═══════════════════════════════
     ▼
  MCP CONTEXT BROKER   auth + ACL filter · search_text / graph.expand / open_evidence
                       per-run & per-agent TOKEN BUDGETS · dedupe + rerank → 3–5 cards
                       writes a retrieval_event for every call
     │  Evidence Pack (L0/L1 cards first; raw L2+ text only by handle)
     ▼
  AGENTS (orchestrator + subagents)   served in VS Code / Copilot / Claude Code
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
  VERIFY   context.verify_answer — L0 provenance check + receipt; every claim cites evidence IDs
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
`glob` tools; `kb_search` carries a per-task token/call cap enforced in the tool, not the prompt. Code
the agent reads arrives **skeleton-first** (ADR-0026) — signatures and types kept, bodies elided — with
the exact body one `read_full` away. The full broker pipeline (`context.create_pack → open_evidence →
verify_answer`) remains available as the *governed* path, but is no longer the only way to read code.

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

### Crash durability (ADR-0027)
The build is one transaction committed at the end, so the artifact-coupled `generation_cache` /
`embedding_cache` are only as durable as that final commit — a mid-build crash rolls them back and the
re-run re-pays the model. A **side-committed, content-keyed model-output cache** (`doc_extraction_output`,
`embedding_output`) persists the *raw* model outputs the moment they are produced, decoupled from
build-scoped artifacts, so a crashed-and-restarted build re-maps them into a fresh `build_seq` with
**zero model calls**. Activation stays atomic; the durable cache is pure derived data, never served.

## 8. MCP Context Broker

The broker is the policy/retrieval/dedupe/evidence/budget layer. Tools:

- `context.create_pack(run_id, task, approved_context_plan, retrieval_profile, budget)` →
  `context_pack_id, kb_version, summary, evidence_cards, open_questions, budget_used`.
- `context.read_pack(context_pack_id, role)` → role-specific view.
- `context.request_more(context_pack_id, agent_name, question, why_needed, decision_needed,
  already_checked_evidence_ids, max_tokens)` → `reused_evidence_ids, new_evidence_cards,
  status ∈ {approved, reused, denied, needs_human_approval}`.
- `context.open_evidence(context_pack_id, evidence_id, max_tokens)` → expand a card.
- `graph.get_neighbors(artifact_id, edge_types, depth)` → related artifacts from Postgres edges.
- `ledger.list_retrievals(run_id)` → retrieval events, cache hits, tokens, evidence used.

## 9. Evidence Packs

A run-scoped shared context object all subagents use, so they don't each form a different worldview.
Sections: known_facts, constraints, relevant_concepts, relevant_code_symbols, relevant_tests,
open_questions. Each evidence card has id, type, title, summary, confidence, authority_score,
`tokens_if_expanded`.

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
real limits even if a prompt fails. Roles: Orchestrator, Implementation, Test Layer, Code Reviewer,
Delivery Planner, PR Planner. Manifests live in `agents/` with strict output schemas and evidence
rules (canon, with `.copilot`/`.opencode` renderings kept at parity).

**KB-first, file-fallback (ADR-0025).** Specialists keep their native `read`/`grep`/`glob` tools (and
`edit` for implementers); the KB is an **optional, budgeted tool**, never a gate that removes the
model's hands. Each manifest expresses the preference: ask the KB first (`kb_search` / structural
lookups), use and cite it when it suffices, and read specific files directly only when the KB is
missing/partial/stale or exact current code is needed. The single enforced restriction is the
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

### Core metrics
`context_tokens_per_successful_task`, `duplicate_context_tokens`, `evidence_reuse_rate`,
`retrieval_calls_per_agent`, `semantic_cache_hit_rate`, `llm_calls_per_build`,
`embedding_calls_per_build`, `unsupported_claim_rate`, `human_plan_edit_rate`,
`missing_context_rate`, `active_kb_age`.

### Evaluation set (build before expanding autonomy)
Plan a new endpoint following existing patterns · find auth/validation rules for a user-scoped
endpoint · identify service/repository files impacted by an embeddings change · find tests covering a
similar endpoint · find release/monitoring guidance for a new route · detect conflicting evidence
between wiki and current code. Each case lists expected docs, files, symbols, tests, and open
questions.

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
