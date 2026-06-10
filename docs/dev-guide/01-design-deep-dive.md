# 01 — Platform design deep dive

> Audience: an engineer who just joined and has never seen this codebase. After reading this you
> should understand *what* we are building, *why* it is shaped this way, and *where* each design
> rule is enforced in code. The implementation walkthrough is in
> [02 — Implementation tour](02-implementation-tour.md).

## What we are building, in one paragraph

A knowledge platform that helps AI coding agents plan and execute software work *without* each
agent re-reading the world on every task. A nightly build ingests code, wikis, docs, and ADO cards
into a **Postgres Knowledge Registry** (artifacts + graph edges), enriches them with LLM-generated
semantic knowledge (**Wikify**) and code-structure knowledge (**Graphify**), links the two layers
together (**Linker**), and projects the result into Azure AI Search for retrieval. At runtime, a
remote **MCP Context Broker** serves that knowledge to a human-approved orchestrator and its
subagents through one shared, budgeted **Evidence Pack**. The pattern is *not* "many agents with KB
access" — it is **"many controlled specialists using one shared Evidence Pack governed by an MCP
Context Broker."**

Canonical reference: `docs/architecture/00-overview.md`. Decisions: `docs/adr/0001`–`0007`.
Build units: `docs/pr-briefs/PR-01`–`PR-13`.

## The two planes

| Plane | What it does | Where it lives | Status |
|---|---|---|---|
| **Build plane** | Nightly incremental refresh of the KB; activates a new `kb_version` only after validation | `apps/kb-builder` | Implemented through PR-08 (connectors → build engine → wikify → graphify → linker → search indexer) |
| **Runtime plane** | Serves agent requests through MCP: evidence packs, budgets, graph traversal, retrieval ledger | `apps/mcp-server` | Not yet built (PR-09/PR-10) |

Shared between them: `packages/contracts` (the schema boundary), `packages/db` (the registry),
`packages/common` (hashing, logging, token budgeting).

## Why Postgres-first (ADR-0002, ADR-0003)

Postgres is the **single source of truth** for artifacts, edges, caches, build runs, and the
retrieval ledger. Azure AI Search is a *derived, rebuildable projection* — if the index is lost or
drifts, we rebuild it from Postgres + source pointers. This kills an entire class of consistency
problems: there is never a question about which store is right.

The graph is the same story (ADR-0003): edges live in a plain `knowledge_edge` table, and graph
*behavior* (neighbors, traversal) will be exposed only through MCP tools. The graph **abstraction**
is V1; a graph **database** is not. If deep traversal ever justifies one, the MCP tool surface is
the seam where the backend swaps.

## Why nightly + incremental (ADR-0004)

Freshness-by-the-minute is not worth an event-driven cloud. The build runs nightly and is
**incremental at every level**:

1. **Source level** — connectors compute a deterministic `content_hash` per source; if it matches
   `source_item.content_hash`, the source is skipped entirely (no chunking, no LLM, no embedding).
2. **Generation level** — every LLM call is gated by `generation_cache`. The cache key encodes
   *all generation inputs* (source content hash, chunker version, prompt version, model name,
   model params hash, output schema version). Hit ⇒ reuse the previously produced artifacts.
3. **Embedding level** — every embedding call is gated by `embedding_cache`, keyed on
   `(artifact_id, text_hash, embedding_model)`. Hit ⇒ no re-embed.

The non-negotiable rule (invariant 4): **cache hit ⇒ no model call.** Cost control is structural,
not a prompt suggestion.

## kb_version lifecycle (invariant 5)

Every build run gets a `kb_version`. A version becomes **active** only after validation passes
(`apps/kb-builder/src/kb_builder/build/active_version.py` — `activate_kb_version` takes a
`ValidationHook`; on failure the run is marked `validation_failed` and the *previous* active
version keeps serving). MCP always serves the last successful active version. A partial unique
index on `kb_build_run` guarantees at most one active run at a time.

One subtlety worth knowing: `kb_version` on an artifact/edge means *the build that produced or last
confirmed it*, not "the only version it belongs to." Cache-hit artifacts keep their original
`kb_version`; linker edges are refreshed in place each night with the confirming build's version.
Since PR-08 the validation hook is real: `make_consistency_validator` compares the Search index
against the registry (missing / orphaned / drifted documents) and a version cannot activate while
the projection disagrees with the truth. Serving semantics consolidate further with the runtime
(PR-09+).

## The knowledge model

**Artifacts** (`knowledge_artifact`) are typed units of knowledge:

- From connectors/chunker: `chunk`
- From Wikify (LLM): `concept`, `summary`, `source_backed_fact`
- From Graphify (deterministic): `code_file`, `code_symbol`, `endpoint`, `test`
- From the MCP runtime (later): `evidence_card`

Each carries `knowledge_kind`: **`interpreted`** (LLM-generated; must rank *below* source-backed
evidence at retrieval time — generated summaries are never treated as truth) or **`source_backed`**
(extracted directly from a source at a version).

**Edges** (`knowledge_edge`) connect artifacts with `edge_type`, `confidence`, `source`
(wikify|graphify|linker|manual), and `kb_version`. Direction is subject-verb-object:

- `doc documents concept`, `card requests concept` — linker, confidence 0.9
- `symbol implements concept` — linker, 0.95 deterministic / raw similarity if semantic
- `doc mentions code` — linker, 0.9
- `symbol exposed_as endpoint`, `test tests symbol`, `calls`, `imports` — graphify, confidence 1.0

Confidence is honest: deterministic exact-text matches get high fixed confidence; semantic
similarity is stored as the raw score, never inflated. Anything below 0.9 is structurally flagged
(`event=linker_low_confidence_edge`) so the eval harness can audit it.

## The three enrichment layers

- **Wikify** (semantic layer): chunks docs/wiki/cards and asks the model for concepts, summaries,
  and source-backed facts. Every call goes through the generation cache. Its output is
  *interpreted* knowledge.
- **Graphify** (code-structure layer): parses code at a commit SHA into files, symbols, endpoints,
  tests, and structural edges. Deterministic — no LLM. It is a navigation aid; final evidence is
  exact snippets at a source version (`span_start`/`span_end` line spans on code artifacts).
- **Linker** (the bridge): connects Wikify concepts to Graphify code. Deterministic pass first
  (exact, word-boundary textual evidence — precision-biased, because over-linking is the explicit
  failure mode), then a semantic-similarity fallback *only* for concepts the deterministic pass
  could not link. Linker edges are **reconciled, one row per logical link**: reruns refresh
  confidence/kb_version in place, and edges whose textual evidence disappears are deleted — the
  graph never serves a link whose justification no longer exists.

The canonical chain these layers produce together:

```
Concept "User Embeddings"
  ← documents  — wiki summary          (linker)
  ← requests   — ADO card summary      (linker)
  ← implements — EmbeddingService.get_user_embedding   (linker)
                  — exposed_as → GET /users/{userId}/embeddings   (graphify)
                  ← tests      — test_get_user_embedding_returns_vector (graphify)
```

## The contract boundary

`packages/contracts` is the schema boundary between everything. Build stages exchange **frozen
pydantic models** (`extra="forbid"`, explicit `schema_version`), not loose dicts: connector output
(`NormalizedContent`), wikify/graphify/linker drafts (`*Draft` models), and later the MCP
request/response schemas. The rule is **contracts before code**: any new tool or artifact gets its
schema written/confirmed in contracts first, then implemented against it. This is what keeps the
two planes and the future MCP clients honest with each other.

## External systems sit behind interfaces

Nothing in the build or (future) tool code imports an Azure SDK directly. Every external dependency
is a `Protocol` the caller owns:

- `ModelClient` — Azure OpenAI (wikify generation)
- `Embedder` — embedding endpoint
- `SearchIndexer` / `SearchClient` — Azure AI Search (`common/search/azure.py` is the *only*
  module allowed to import the SDK; `FakeSearchClient` is the in-memory stand-in)
- `SimilarityProvider` — vector similarity for the linker's semantic pass

Two payoffs: tests are hermetic (in-memory fakes, no cloud), and each backend is swappable via ADR
without touching call sites. This is also why **everything through PR-07 runs locally with zero
Azure resources** — see [03 — Local testing](03-local-testing.md).

## Security posture (enforced at the MCP boundary, PR-09+)

Agents never touch data stores or secrets directly; all retrieval is mediated by MCP. Retrieved
documents are **untrusted content** — they cannot change tool policy, identity, access control, or
instructions. Every agent claim must cite evidence IDs; missing evidence becomes an open question,
never an invention (invariant 7 — this is also why the linker deletes edges whose evidence is gone
rather than serving them).

Since PR-09 the first layer is real: every MCP request must carry an Entra ID bearer token,
verified against the tenant's public JWKS keys (`mcp_server/auth/entra.py`) — the server stores no
client secret. Telemetry attributes each call to the *verified* token subject, never to a
client-asserted field, and the contracts encode policy directly: `context.request_more` requires a
full justification (a bare `{"query": ...}` fails schema validation), and expanded evidence is
delivered in a field literally named `untrusted_content`. Only `/health` is unauthenticated, and
it discloses nothing but the service name and active kb_version.

## What is deliberately NOT in V1 (ADR-0007)

Azure Functions, Event Grid/Service Bus/Event Hub, Redis, API Management, Blob Storage, a graph
database, local SQLite as a production store, real-time ingestion, unrestricted subagent search.
Each has a written trigger condition for when it may be added — behind the existing MCP interface,
via a new ADR. Default answer is no.

## Invariants → enforcement map

| # | Invariant | Enforced by |
|---|---|---|
| 1 | Postgres is truth; Search is a projection | ADR-0002; `kb_builder/indexer/consistency.py` drift check gates activation; embedding vectors stored in `embedding_cache` so the index rebuilds without re-embedding |
| 2 | Graph in Postgres, behavior via MCP tools | `knowledge_edge` table; `graph.get_neighbors` contract + registered stub (PR-09), broker logic PR-10 |
| 3 | Token saving enforced by the broker, not prompts | Context Broker budgets + ledger (PR-10); `.claude/rules/token-budgets.md` |
| 4 | Incremental build; cache hit ⇒ no model call | `GenerationCacheGate` / `EmbeddingCacheGate` in `apps/kb-builder/src/kb_builder/build/cache.py`; content-hash skip in `build/runner.py` |
| 5 | kb_version active only after validation | `build/active_version.py` + unique partial index on `kb_build_run` |
| 6 | Agents never touch stores/secrets; retrieved text untrusted | Entra JWKS auth boundary + schema-encoded policy in `contracts/mcp_schemas` (PR-09); hardening PR-13 |
| 7 | Every claim cites evidence; no fabrication | Evidence-ID discipline (PR-10/11); linker stale-edge deletion in `linker/write_edges.py` |
