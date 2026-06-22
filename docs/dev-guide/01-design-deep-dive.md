# 01 — Platform design deep dive

> Audience: an engineer who just joined and has never seen this codebase. After reading this you
> should understand *what* we are building, *why* it is shaped this way, and *where* each design
> rule is enforced in code. The implementation walkthrough is in
> [02 — Implementation tour](02-implementation-tour.md).

## What we are building, in one paragraph

A knowledge platform that helps AI coding agents plan and execute software work *without* each
agent re-reading the world on every task. A nightly build ingests code, wikis, docs, and ADO cards
into a **Postgres Knowledge Registry** (artifacts + graph edges), enriches them with LLM-generated
semantic knowledge (**Docify**) and code-structure knowledge (**Graphify**), links the two layers
together (**Linker**), and projects the result into Azure AI Search for retrieval. At runtime, a
remote **MCP Context Broker** serves that knowledge to a human-approved orchestrator and its
subagents through one shared, budgeted **Evidence Pack**. The pattern is *not* "many agents with KB
access" — it is **"many controlled specialists using one shared Evidence Pack governed by an MCP
Context Broker."**

Canonical reference: `docs/architecture/00-overview.md`. Decisions: `docs/adr/0001`–`0015`.
Build units: `docs/pr-briefs/PR-01`–`PR-33`.

## The two planes

| Plane | What it does | Where it lives | Status |
|---|---|---|---|
| **Build plane** | Nightly incremental refresh of the KB; activates a new `kb_version` only after validation + publish gates | `services/kb-builder` | Implemented through PR-33: connectors (local-FS + production GitHub/ADO, ADR-0015) → build engine → docify (Graphify LLM doc extraction, ADR-0023) → graphify (whole-tree extractor) → linker (deterministic + cross-domain + candidate→LLM judge) → version-membership invalidation → search indexer → enforcing publish gates; a single `build` CLI (`python -m agentic_kb_builder.build`) drives it end to end |
| **Runtime plane** | Serves agent requests through MCP: evidence packs, budgets, trust-aware graph traversal, the verifier ladder + signed receipts, client identity, intent-aware ranking, retrieval ledger | `services/mcp-server` | Implemented (PR-09 server base; PR-10 Context Broker; PR-11 agent manifests + output schemas; PR-13 security hardening: `team_acl_v1` filtering, injection flagging, audit logging; PR-23/24/30/31 the verifier ladder L0→L3 + signed receipts; PR-32 client/app identity + scopes; PR-33 temporal/intent ranking) |
| **Benchmark layer** | Dev-only eval harness: runs the §13 benchmark cases through the real broker, computes token-cost + golden-query evidence-recall metrics, diffs against a committed baseline | `evals/` | Implemented (PR-12; golden queries PR-25; contracts in `docs/contracts/evals-report.md` + `golden-query-evals.md`) |

Nothing is shared at runtime (ADR-0008): each service is a self-contained `uv` project, and the
only cross-service agreements are the markdown contracts in `docs/contracts/`, pinned by contract
tests on both sides. kb-builder owns the Postgres schema and all Alembic migrations.

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

## kb_version lifecycle: interval membership, not a label (invariant 5, ADR-0013)

Every build run gets a `kb_version` label and a monotonic `build_seq` (a `BIGINT` from the
`kb_build_seq` Postgres sequence, assigned at run start). A version becomes **active** only after
validation **and the publish gates** pass
(`services/kb-builder/src/agentic_kb_builder/application/active_version.py` — `activate_kb_version`
takes a `ValidationHook`; on failure the run is marked `validation_failed` and the *previous*
active version keeps serving). MCP always serves the last successful active version. A partial
unique index on `kb_build_run` guarantees at most one active run at a time.

**A KB version is a validity interval, not a creation label** (`docs/contracts/version-membership.md`).
This fixes a latent bug ADR-0013 records: incremental builds re-create only *changed* rows (label
`N`), so the unchanged majority keep labels `< N`. Scoping retrieval strictly `WHERE kb_version = N`
would serve only that day's delta — the served KB would silently shrink every night. The fix: each
`knowledge_artifact` / `knowledge_edge` carries `valid_from_seq` (the build that introduced it) and
`invalidated_at_seq` (`NULL` while live; set to build `N` when the row *leaves* the KB). A row is a
**member of the version whose `build_seq = S`** iff:

```
valid_from_seq <= S AND (invalidated_at_seq IS NULL OR invalidated_at_seq > S)
```

Both services duplicate this one predicate (never share the code — ADR-0008). Rows are stamped once
and immutable; setting `invalidated_at_seq` never mutates a *past* version, so prior active versions
stay byte-reconstructable (invariant 5). The lone deliberate exception is `acl_teams`, which is
overwritten in place on live rows so a *revoked* permission takes effect on every still-served
version, not just future ones — fail-safe current enforcement over exact historical-ACL replay.

At the end of each build, **before** activation, the invalidation pass
(`application/invalidation.py`) reconciles identity-over-time: rename detection (a vanished identity
whose content hash reappears at a new path links via `prior_identity_id` and reattaches its edges),
a deletion sweep (a source no longer listed is marked `is_deleted` and its live rows invalidated), a
supersession sweep (a content-changed source's prior-generation rows are invalidated so the new
version serves only the new generation), and ACL propagation.

**Publish gates** (`application/publish_gates.py`, `docs/contracts/publish-gates.md`) make the
activation decision deterministic. The phase-1 gates — index consistency, extractor error rate,
symbol-count delta, no dangling citations, edge-evidence integrity — are real and **enforcing**
inside activation; the `no-ghost-edges` gate became enforcing once membership was interval-based
(PR-27). The first failing, non-overridden gate records which gate + its measured value on
`kb_build_run` and the version simply never activates (automatic rollback — the previous active row
is untouched). `--allow-large-delta` is the *only* overridable gate (symbol-count delta), recorded
and logged. Evidence-recall + ACL-leak are authoritatively enforced by the evals harness
(`make eval-run`) because they need the full broker over the golden set, which `kb-builder` cannot
import across the service boundary; activation logs a registry-derivable proxy.

`make_consistency_validator` (the index-vs-registry drift check) is composed into the gate
`ValidationHook` by `make_publish_gate_validator`, so a version cannot activate while the Search
projection disagrees with the truth.

## The knowledge model

**Artifacts** (`knowledge_artifact`) are typed units of knowledge:

- From Docify (Graphify LLM doc extraction, ADR-0023): `concept`, `summary`, `source_backed_fact`
- From Graphify (deterministic whole-tree extractor): `code_file`, `code_symbol`, `endpoint`, `test`
- From the MCP runtime (later): `evidence_card`

Each carries `knowledge_kind`: **`interpreted`** (LLM-generated; must rank *below* source-backed
evidence at retrieval time — generated summaries are never treated as truth) or **`source_backed`**
(extracted directly from a source at a version).

**Edges** (`knowledge_edge`) connect artifacts with `edge_type`, `confidence`, `source`
(graphify|linker|llm_judge|manual), a `trust_class` (below), `relation_schema_version`, an
`evidence` pointer, and the `valid_from_seq`/`invalidated_at_seq` interval. Direction is
subject-verb-object:

- `doc documents concept`, `card requests concept` — linker, confidence 0.9
- `symbol implements concept` — linker, 0.95 deterministic / raw similarity if semantic
- `doc mentions code`, `commit mentions code_file`, `commit implements work_item` — linker, deterministic
- `symbol exposed_as endpoint`, `test tests symbol`, `calls`, `imports` — graphify, confidence 1.0
- `doc documents code` — `llm_judge` (phase 3B), only ever `INFERRED_*`

Confidence is honest: deterministic exact-text matches get high fixed confidence; semantic
similarity is stored as the raw score, never inflated. Anything below 0.9 is structurally flagged
(`event=linker_low_confidence_edge`) so the eval harness can audit it.

### Trust buckets, not decimal confidence (ADR-0011, `docs/contracts/trust-buckets.md`)

Behaviour is driven by a small set of **trust buckets** on every edge (`trust_class`, NOT NULL,
CHECK-constrained), derived *deterministically* from the producing mechanism — never a free-floating
score: `EXTRACTED` < `INFERRED_HIGH` < `INFERRED_LOW` < `AMBIGUOUS` < `REJECTED` (ordering for
`trust_floor`). Deterministic producers (the AST extractor, the deterministic linker) may assign
**only** `EXTRACTED`; the LLM judge (phase 3B) may assign any `INFERRED_*` / `AMBIGUOUS` / `REJECTED`
but **never** `EXTRACTED`. The buckets change broker behaviour:

- `EXTRACTED` is included in default traversal and is the **only** bucket that can support a cited,
  platform-trusted claim.
- `INFERRED_HIGH` / `INFERRED_LOW` are surfaced only with `include_inferred=true`, always as
  **routing hints** (`claim_supporting=false`) — they point an agent at source evidence to read, but
  can never themselves be the cited support for a claim.
- `AMBIGUOUS` is excluded from default traversal; `REJECTED` is never returned (retained for audit).

## The three enrichment layers

- **Docify** (semantic layer, ADR-0023): routes docs/wiki/cards through Graphify's LLM doc pipeline
  (`graphify.llm.extract_files_direct`) behind a thin `docify` adapter, configured from the same
  `LLM_*` env as every other model call. It produces the same artifact shapes as before — `summary`
  (interpreted), `concept` (interpreted), `source_backed_fact` (source_backed); only the producer
  changed (hand-rolled prose LLM → Graphify LLM). Trust is re-derived deterministically: a concept
  whose supporting sentence is a verbatim substring of the source (same whitespace-normalization as
  the broker's L0 verifier) becomes a citable `source_backed_fact`, otherwise an `interpreted`
  concept; the document node becomes an interpreted `summary`. Every call goes through the generation
  cache (no model call on unchanged docs); it writes **artifacts only** (no concept→concept edges —
  generic relatedness is banned by the relation ontology).
- **Graphify** (code-structure layer): the Graphify library runs **once per repo** (`graphify_tree`)
  over code at a commit SHA, resolving cross-file imports/calls/uses natively and yielding files,
  symbols, endpoints, tests, and structural edges (`defined_in`, `calls`, `imports`, `inherits`,
  `uses`, `references` — all `EXTRACTED`, `source='graphify'`) — no per-file extractor, no hand-rolled
  import linker (ADR-0012 / ADR-0018). We still recover exact symbol spans ourselves (Graphify reports
  only a start line). It is a navigation aid; final evidence is exact snippets at a source version
  (`span_start`/`span_end` line spans on code artifacts).
- **Linker** (the bridge): three stages, all precision-biased because over-linking is the explicit
  failure mode.
  1. **Deterministic** (`linker/deterministic.py`): exact, word-boundary textual evidence connecting
     Docify concepts to Graphify code. `EXTRACTED`.
  2. **Cross-domain deterministic** (`linker/cross_domain.py`, PR-26): zero-LLM, explicit-reference
     only — commit→work-item (`AB#123`/`#123`/`GH-123`), commit→code_file (changed-file path), and
     doc→work-item. A bare incidental number never produces a link. The `commit` artifacts come from
     the `git_metadata` connector. `EXTRACTED`.
  3. **Candidate→judge** (PR-28/29): a cheap, deterministic, zero-LLM **candidate generator**
     (`linker/candidates.py`) surfaces bounded cross-domain pairs (top-K per artifact, audited in
     `relationship_candidate`); the **LLM judge** (`linker/judge.py`) rules on *only* that bounded
     set — never a global O(N²) sweep — and promotes verdicts to `INFERRED_*`/`AMBIGUOUS`
     `knowledge_edge` rows (`source='llm_judge'`). Every judgment is gated by
     `relationship_judgment_cache` so an unchanged pair is never re-judged, and a `supporting_quote`
     must be a verbatim substring of a source span or the verdict is downgraded to `AMBIGUOUS`
     (invariant 7). A semantic-similarity fallback also exists for concepts the deterministic pass
     could not link (raw score as confidence).

  Linker edges are **reconciled, one row per logical link**: reruns refresh confidence/version in
  place, and edges whose textual evidence disappears are deleted — the graph never serves a link
  whose justification no longer exists.

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

Contracts live in two layers. *Within* a service, build stages exchange **frozen pydantic
models** (`extra="forbid"`, explicit `schema_version`), not loose dicts: connector output
(`NormalizedContent`) and docify/graphify/linker drafts (`*Draft` models) in
`agentic_kb_builder/domain/`, and the MCP request/response schemas in
`agentic_mcp_server/mcp/tool_schemas/`. *Between* the services, the contract is markdown:
`docs/contracts/` records the registry tables, the search index shape, the Evidence Pack, the
MCP tool surface, and agent output rules — and contract tests in each service pin their code to
those documents. The rule is **contracts before code**: any new tool or artifact gets its schema
written/confirmed first, then implemented against it.

## External systems sit behind interfaces

Nothing in the build or (future) tool code imports an Azure SDK directly. Every external dependency
is a `Protocol` the caller owns:

- `ModelClient` — chat model behind `LLM_*` env (docify doc extraction, relationship judge)
- `Embedder` — embedding endpoint
- `SearchIndexer` / `SearchClient` — Azure AI Search
  (`agentic_kb_builder/infrastructure/azure_search/azure_search_client.py` is the *only* module
  allowed to import the SDK; `FakeSearchClient` is the in-memory stand-in)
- `SimilarityProvider` — vector similarity for the linker's semantic pass

Two payoffs: tests are hermetic (in-memory fakes, no cloud), and each backend is swappable via ADR
without touching call sites. This is also why **everything through PR-07 runs locally with zero
Azure resources** — see [03 — Local testing](03-local-testing.md).

## Security posture (enforced at the MCP boundary, PR-09+)

Agents never touch data stores or secrets directly: they work in a scoped workspace with native
`read`/`grep`/`glob` plus the budgeted `kb_search`, and never hold Postgres/Search/model credentials.
Governance lives at the identity/ACL + ledger layers, not in a mandatory broker round-trip — the KB is
**preferred-first and budgeted, not a gate** (ADR-0025), and a file-fallback is logged as a KB-gap
signal. The governed `create_pack → open_evidence → verify_answer` path stays available where a claim
must be citation-grade. Retrieved documents are **untrusted content** — they cannot change tool policy,
identity, access control, or instructions. Every agent claim that *is* served as evidence must cite
evidence IDs; missing evidence becomes an open question, never an invention (invariant 7 — this is also
why the linker deletes edges whose evidence is gone rather than serving them).

Since PR-09 the first layer is real: every MCP request must carry an Entra ID bearer token,
verified against the tenant's public JWKS keys (`agentic_mcp_server/auth/entra.py`) — the server stores no
client secret. Telemetry attributes each call to the *verified* token subject, never to a
client-asserted field, and the contracts encode policy directly: `context.request_more` requires a
full justification (a bare `{"query": ...}` fails schema validation), and expanded evidence is
delivered in a field literally named `untrusted_content`. Only `/health` is unauthenticated, and
it discloses nothing but the service name and active kb_version.

## The end-to-end trust contract (ADR-0011)

The broker governs *retrieval*. It does **not** govern an external agent's final *answer* — Copilot,
Claude, or OpenCode can ignore the evidence or misread it. The only enforceable boundary against
agents we don't control is: **an answer is platform-trusted iff it carries a valid verification
receipt.** Four pieces make that real (contracts: `trust-buckets.md`, `verification-receipt.md`,
`acl-source-visibility.md`):

- **Trust-aware traversal** — `graph.get_neighbors` takes `trust_floor` (default `EXTRACTED`) and
  `include_inferred` (default `false`); the default result is exactly the directly-extracted graph,
  and `INFERRED_*` edges come back only as labelled routing hints. Trust filtering composes with the
  ACL filter at *every* hop.
- **The verifier ladder** (`context.verify_answer`) checks an answer's cited claims against the
  ledger in escalating cost — cheap, deterministic levels first, the model last and only when
  unavoidable:
  - **L0** (deterministic, mandatory, PR-24): each cited evidence id exists, is in the active version,
    is ACL-visible, appears in the requester's retrieval ledger, is not stale, and is supported by an
    `EXTRACTED` edge (an `INFERRED_*` hint cannot be the sole support).
  - **L1** (PR-30): citation coverage + quote span caps.
  - **L2** (PR-30, no LLM): typed-fact adjudication — catches a claim that *misreads* real evidence
    (`symbol_in_file`, `file_imports_module`, `edge_between`), checked against a claim/evidence
    ledger projected over the existing tables.
  - **L3** (PR-31, cached LLM entailment): runs **only** for claims L0–L2 could not adjudicate, and
    every entailment is gated by `entailment_cache` so an unchanged claim makes zero model calls
    (invariant 4).
- **Signed receipts** (PR-31) — the verifier returns a receipt (`answer_hash`, per-claim results,
  the levels run, `graph_version`) signed with **HMAC-SHA256** under a key read from an env var by
  *name* (default `VERIFY_SIGNING_KEY`); a host validates it statelessly with no DB and no re-checks.
- **Client/app identity + scopes** (PR-32) — a request carries both the per-user `Requester` and a
  registered `ClientIdentity` (`client_id`, `scopes`, `verification_required`), resolved from the
  authenticated client credential, never a request field (config: `MCP_CLIENT_REGISTRY`, identifiers
  + policy only). Client scopes gate the tool surface **additively** on top of (never replacing) the
  user team ACLs. The verifier binds the validated `client_id` into the signed receipt, so a receipt
  for client A does not validate for client B. `context.platform_trust` is the official-client gate:
  a `verification_required` client is platform-trusted only with a valid, client-matched, passing
  receipt, and returns a structured denial reason otherwise — never a silent pass. A client that did
  not opt in is unaffected.

**Temporal / intent-aware ranking** (PR-33) rides on top, all deterministic and logged
(`event=temporal_weight*`): each evidence card carries a `source_kind` (code/doc/card/pr/adr) and a
`temporal_state` (current/superseded) derived with no LLM from version-membership + source state.
When `create_pack.intent` is supplied (`how_does_x_work`, `why_was_x_changed`, `who_owns_x`,
`what_calls_x`) the broker *transparently re-weights* the candidate set — current code first for
"how", cards/PRs/ADRs included for "why", stale docs downranked — without ever removing historical
evidence, promoting a contradicting doc into claim support, or touching the L0 `not_stale` check
(the two notions of "stale" are deliberately independent).

## What is deliberately NOT in V1 (ADR-0007)

Azure Functions, Event Grid/Service Bus/Event Hub, Redis, API Management, Blob Storage, a graph
database, local SQLite as a production store, real-time ingestion, unrestricted subagent search.
Each has a written trigger condition for when it may be added — behind the existing MCP interface,
via a new ADR. Default answer is no.

## Invariants → enforcement map

| # | Invariant | Enforced by |
|---|---|---|
| 1 | Postgres is truth; Search is a projection | ADR-0002; `agentic_kb_builder/indexing/consistency.py` drift check gates activation; embedding vectors stored in `embedding_cache` so the index rebuilds without re-embedding |
| 2 | Graph in Postgres, behavior via MCP tools | `knowledge_edge` table (now with `trust_class`); trust-aware `graph.get_neighbors` bounded BFS in the broker (PR-10, PR-23) |
| 3 | Token saving enforced in code (budget + compression), not prompts; KB preferred-first, not a gate | `kb_search` per-task call+token cap in the tool (ADR-0025); Context Broker budgets + ledger under a per-pack lock (PR-10); `entailment_cache` gates L3 model calls (PR-31); deterministic skeleton-first reads + `read_full` (ADR-0026, `scripts/codeskeleton.py`); `.claude/rules/token-budgets.md` |
| 4 | Incremental build; cache hit ⇒ no model call | `GenerationCacheGate` / `EmbeddingCacheGate` in `agentic_kb_builder/application/cache_gates.py`; `relationship_judgment_cache` (PR-29) + `entailment_cache` (PR-31); content-hash skip in `application/build_runner.py` |
| 5 | kb_version active only after validation + publish gates | `application/active_version.py` (interval membership, ADR-0013) + `application/publish_gates.py` + `application/invalidation.py` + unique partial index on `kb_build_run` |
| 6 | Agents never touch stores/secrets (scoped workspace + budgeted KB, not a mandatory broker round-trip); retrieved text untrusted | No DB/Search/model credentials agent-side; governance at the identity/ACL + ledger layers with file-fallback logged as a KB-gap signal (ADR-0025); Entra JWKS auth boundary + schema-encoded policy in `agentic_mcp_server/mcp/tool_schemas` (PR-09); `team_acl_v1` filtering at every retrieval surface, fail-closed identity, advisory injection flagging, and audit logging in `auth/rbac.py` / `domain/untrusted.py` / `telemetry/audit.py` (PR-13) |
| 7 | Every claim cites evidence; no fabrication | Evidence cards by handle (PR-10); `agent_output_schemas` make unevidenced claims unconstructible and reject unknown evidence IDs (PR-11); linker stale-edge deletion in `linker/write_edges.py` |
