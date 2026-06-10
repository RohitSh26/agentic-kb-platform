# Architecture Overview — Agentic KB Platform (V1)

> Canonical, distilled reference. Source: `Agentic Knowledge-Based AI System — Architecture and
> Implementation Blueprint v0.1`. Agents, skills, and PR briefs reference this file by section name.

## 1. Thesis

An agentic system that helps developers plan and execute software work using a centrally managed
knowledge base. A human-approved **orchestrator** plans the work; specialized **subagents** execute
against one shared **Evidence Pack** governed by a remote **MCP Context Broker**; a **nightly-built**
KB combines semantic knowledge (Wikify) with code-structure knowledge (Graphify) in one canonical
**Postgres** registry, projected into **Azure AI Search** for retrieval.

The design is deliberately lean: no event-driven cloud, no Redis, no API Management, no Blob (by
default), no graph database, no local SQLite in production. Those are deferred until real pressure
justifies them (see ADR-0007).

## 2. Two planes

- **Runtime plane** — serves agent requests through MCP. Developer ⇄ AI coding client ⇄ orchestrator
  ⇄ MCP Context Broker ⇄ {Postgres truth, Azure AI Search projection, model endpoint}.
- **Build plane** — refreshes the KB nightly and activates a new `kb_version` only after validation.

## 3. Developer experience

Developers install nothing about the KB. They need: an AI coding client, agent markdown files in the
repo, MCP config to the remote server, company SSO, and a local repo checkout. No local KB, vector
DB, Graphify, Search keys, or model keys on developer machines.

Flow: ask orchestrator → orchestrator drafts a plan (goal, subagents, context needed, retrieval
budget) → human approves/edits → orchestrator calls `context.create_pack` → MCP builds a shared
Evidence Pack → subagents get role-specific views → subagents request justified deltas within budget
→ orchestrator synthesizes a phased PR plan with evidence IDs, risks, open questions.

## 4. Knowledge Base design

The KB is a Postgres-backed Knowledge Registry with graph-shaped relationships and an Azure AI Search
projection — not "just a vector index."

### Artifact types
`source_item` (connectors), `chunk` (chunker), `concept` / `summary` / `source_backed_fact`
(Wikify), `code_file` / `code_symbol` / `endpoint` / `test` (Graphify), `evidence_card` (MCP runtime).

### Edge types
`documents`, `implements`, `calls`, `imports`, `tests`, `requests`, `mentions`, `depends_on`,
`exposed_as`. Each edge carries `confidence`, `source` (wikify|graphify|linker|manual), `kb_version`.

> Graph decision: the graph abstraction is V1; the graph database is not. Edges live in Postgres;
> graph behavior is exposed through MCP tools.

## 5. Wikify / Graphify / Linker

- **Wikify** = semantic layer. Inputs: docs, wiki, ADO cards, selected code comments. Outputs:
  concepts, summaries, source-backed facts, rollups, evidence-ready chunks. Risk: generated summaries
  are interpreted knowledge — rank below current source-backed evidence.
- **Graphify** = code-structure layer. Inputs: code at commit SHA. Outputs: files, symbols,
  endpoints, imports, call edges, test links, service boundaries. Risk: it is a navigation aid; final
  evidence uses exact snippets at a source version.
- **Linker** connects Wikify concepts to Graphify code via deterministic matching, source refs, path
  conventions, embedding similarity, and limited LLM help. Example:
  `Concept: User Embeddings → documents → Wiki; → requested_by → ADO card; → implemented_by →
  EmbeddingService.get_user_embedding; → exposed_as → GET /users/{userId}/embeddings; → tested_by →
  test_get_user_embedding_endpoint`.

## 6. Postgres Knowledge Registry (schema sketch)

```
source_item(source_id PK, source_type, source_uri, source_version, repo?, branch?, path?,
            external_id?, content_hash NOT NULL, last_seen_at, is_deleted=false, created_at, updated_at)

knowledge_artifact(artifact_id PK, artifact_type, source_id FK, title, body_text, content_hash,
                   artifact_hash, kb_version, authority_score, freshness_score, created_at, updated_at)

knowledge_edge(edge_id PK, from_artifact_id FK, to_artifact_id FK, edge_type, confidence, source,
               kb_version, created_at)

generation_cache(cache_key PK, input_hash, prompt_version, model_name, model_params_hash,
                 output_schema_version, output_artifact_id FK, created_at)

embedding_cache(artifact_id FK, text_hash, embedding_model, embedding_hash, azure_search_doc_id,
                created_at, PRIMARY KEY(artifact_id, text_hash, embedding_model))

kb_build_run(build_id PK, kb_version, status, started_at, completed_at, sources_seen,
             sources_changed, artifacts_created, artifacts_updated, artifacts_deleted, llm_calls,
             embedding_calls, search_docs_upserted, error_summary)

retrieval_event(retrieval_id PK, run_id, context_pack_id, agent_name, tool_name, query_text,
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
chunk, Wikify on generation_cache miss, Graphify for changed code, update artifacts+edges, embed on
embedding_cache miss, upsert changed docs to Search. 7. Validate retrieval/index consistency.
8. Mark new `kb_version` active only if validation succeeds.

### Cache keys
- Chunk summary: `source_content_hash + chunker_version + wikify_prompt_version + model_name +
  model_params_hash + output_schema_version`.
- Concept rollup: `concept_id + sorted_supporting_artifact_hashes + rollup_prompt_version +
  model_name + output_schema_version`.
- Code graph: `repo + commit_sha + file_path + file_content_hash + graphify_version +
  parser_config_version`.

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

## 10. Token-saving controls (enforced in the broker)

Shared Evidence Pack · evidence cards first · per-run budget · per-agent budget · exact query cache ·
semantic query cache · role-specific views · evidence IDs/handles · precomputed summaries ·
AST/symbol extraction · compression cache. Budgets: see `.claude/rules/token-budgets.md`.

Token policy: subagents may not "think by retrieving." Read the pack first, then request only missing
context with a reason and an expected decision. A bare `{"query": "..."}` is rejected.

## 11. Agent design (product runtime)

Markdown agent files behave like manifests, not just prompts; server-side MCP policy enforces limits
even if a prompt fails. Roles: Orchestrator, Implementation, Test Layer, Code Reviewer, Delivery
Planner, PR Planner. Manifests live in `agents/` with strict output schemas and evidence rules.

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
