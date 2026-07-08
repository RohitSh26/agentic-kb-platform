# Contract: MCP tools (Context Broker surface)

> Versioned tool surface served by mcp-server. Schema before code: every tool
> has a frozen pydantic request/response model (`extra="forbid"`) in
> `services/mcp-server/src/agentic_mcp_server/mcp/tool_schemas/`, registered in
> `mcp/tool_registry.py`. `MCP_SCHEMA_VERSION = "1.12.0"` (1.1.0 = PR-13:
> `authorization` decision on every retrieval response, `injection_*` markers
> on cards and expansions; 1.2.0 = PR-18: `read_pack.role` opened from the
> closed six-role enum to a free-form charset-guarded string — response
> consumers may now receive team-defined role values; 1.3.0 = PR-23:
> `graph.get_neighbors` gains `trust_floor` + `include_inferred` (trust-aware
> traversal, ADR-0011) and `GraphNeighbor` gains `trust_class` +
> `claim_supporting`; 1.4.0 = PR-24: adds `context.verify_answer`, the L0
> provenance verifier that returns a verification receipt — additive, the trust
> boundary of ADR-0011 / `verification-receipt.md`; 1.5.0 = PR-30:
> `context.verify_answer` gains optional per-claim `quote` + typed `assertion`
> and accepts `verifier_levels` up to `["L0","L1","L2"]` — the deterministic L1
> (citation coverage + span caps) and L2 (typed-fact) levels add `L1_coverage` /
> `L2_typed_fact` to claim `checks`. All additive: an L0-only caller is
> unchanged; 1.6.0 = PR-31: `context.verify_answer` accepts `verifier_levels`
> up to `["L0","L1","L2","L3"]` — L3 (cached LLM entailment) runs ONLY on claims
> L0-L2 could not adjudicate deterministically and adds `L3_entailment` to claim
> `checks`; the receipt's reserved `signature` is now populated (HMAC-SHA256) and
> a non-secret `key_id` is added so a host can validate it statelessly. All
> additive: an L0-only caller is unchanged; 1.7.0 = PR-32: client/app identity +
> scopes + official-client enforcement (ADR-0011 §6). A request now carries a
> registered **client identity** (resolved from the authenticated client
> credential, NOT a request field) alongside the per-user subject. The verifier
> stamps the validated `client_id` into the receipt AND binds it into the signed
> payload, so a receipt for client A does NOT validate for client B
> (cross-client reuse rejected). Adds `context.platform_trust`, the
> official-client gate: a `verification_required` client is platform-trusted
> ONLY with a valid, client-matched, passing receipt; a clear STRUCTURED denial
> otherwise; a non-opted-in client is unaffected. Client scopes ADDITIVELY gate
> the tool surface and compose WITH (never replace) the user team ACLs. All
> additive: a deployment that ships no client registry is unchanged);
> 1.8.0 = adds `context.expand`: trust-tiered BFS expansion from seed artifact
> ids, returning new evidence cards within a token budget. EXTRACTED backbone
> first, INFERRED tier second (only when `include_inferred=true`). Budget
> enforced; `truncated=true` when the budget cap was hit. If
> `context_pack_id` is given, the expansion is charged against that pack's run
> budget and new cards are registered into it so they are openable by handle.
> Writes a `retrieval_event` row per call; 1.9.0 = PR-37 (ADR-0025, ADR-0030):
> adds `kb_search`, the budgeted KB-first retrieval tool — one `query` in,
> ranked ACL-filtered hits out (`title`, `artifact_type`, `source_uri`,
> `snippet`, `confidence_tier`), with a server-side dual cap (call count AND
> cumulative tokens) per (MCP session, subject). Additive: every `context.*`
> tool is unchanged and stays registered; 1.10.0 = PR-39 (ADR-0030 Decision §2,
> `docs/proposals/2026-07-02-tool-design-first-kb-architecture.md` §2–3): adds
> `get_task_context`, the one-call task-context tool — a task description (plus
> optional file/symbol hints) in; resolved scope, blast radius
> (callers/callees/tests), conventions, and similar prior changes out. Every
> entity carries a `confidence_tier` (`ground_truth | deterministic |
> interpreted`) and a source path; a `calls` edge is `deterministic` ONLY when
> corroborated by the import graph (or same-module definition), otherwise
> `interpreted` with a `caveat` — the 2026-07-02 Graphify-audit rule. Ambiguous
> resolution returns `ambiguous_candidates` + `open_questions`, never a silent
> guess. Zero LLM at query time: the backend is a LangGraph StateGraph of four
> parallel pure-retrieval nodes, a synthesis node, and ONE broadened retry on
> empty scope. Additive: no existing tool changes; 1.11.0 = PR-41 (ADR-0031):
> adds `get_review_draft` — fetch the review-panel's stored draft for a pull
> request (`repo`, `pr_number`, optional `head_sha`; omitted ⇒ the newest
> stored draft for that PR). Read-only and compute-never: the tool NEVER
> triggers draft computation — the `review_panel` Postgres schema stays owned
> and exclusively written by `services/review-panel`
> (`docs/contracts/review-panel.md`); mcp-server only ever `SELECT`s it, over
> its own existing Postgres connection, schema-qualified. No draft yet is a
> clean `{found: false}` response, never a tool error. Carries **no**
> `kb_search`-style budget charge (fetching a stored draft is not knowledge
> retrieval) — the only gates are authentication (same as every other tool)
> and the client-scope check. Additive: no existing tool changes; 1.12.0 =
> PR-42 (ADR-0033, response economy): `get_task_context` adopts
> **cross-section path dedup** — the response gains `referenced_paths`
> (the canonical, deduplicated, lexicographically sorted table of every path
> any blast-radius entry surfaces) and `blast_radius` entries replace
> `path: str` with `path_ref: int`, an index into `referenced_paths`
> (`resolved_scope` entities keep their full `path`; `similar_prior_changes`
> entries carry commit ids, never file paths, so nothing to reference there).
> `budget_used` moves to the END of the response — the documented volatile
> tail — so the stable identifiers serialize first. Both `get_task_context`
> and `kb_search` adopt the **response-stability discipline**: deterministic
> field order, a documented sort for every list, stable identifiers early and
> volatile values late; two identical requests produce byte-identical JSON
> modulo the documented volatile tail (`get_task_context`: `budget_used`;
> `kb_search`: `budget_remaining` + `notice`, which reflect prior window
> usage). `kb_search`'s `snippet` source widens from `body_text` only to
> `body_text` **else** `search_text`, so `code_file` hits (pointer-only,
> `body_text=NULL`) stop serving empty snippets and carry the build-time
> deterministic code skeleton the registry now stores in their `search_text`
> (ADR-0033; skeletons are display/search material — for *thinking*, never
> *citing*). Breaking only in the `get_task_context` wire shape
> (`path` → `path_ref` + `referenced_paths`); every other tool is unchanged.

## The V1 tools

| Tool | Purpose |
|---|---|
| `context.create_pack` | Build the run's Evidence Pack from an approved context plan |
| `context.read_pack` | Role-specific view of an existing pack |
| `context.request_more` | Justified incremental retrieval (reuse-first) |
| `context.open_evidence` | Expand one evidence card to L2/L3 raw text, by handle |
| `graph.get_neighbors` | Graph traversal over `knowledge_edge` (depth 1–3) |
| `ledger.list_retrievals` | Retrieval ledger for a run |
| `context.expand` | Trust-tiered BFS expansion from seed artifact ids; returns evidence cards |
| `context.verify_answer` | L0 provenance verifier; returns a verification receipt |
| `context.platform_trust` | Official-client gate: is the client's answer platform-trusted? |
| `context.create_change_pack` | BUILD-lane selector: the small file set (target/test/dependency) to edit for a code-change task |
| `kb_search` | ADR-0025 KB-first retrieval: one budgeted, ACL-filtered ranked search over the active KB |
| `get_task_context` | One-call task context (ADR-0030): resolved scope + blast radius + conventions + similar prior changes, tiered, cited, budgeted |
| `get_review_draft` | Fetch the review-panel's stored draft for a pull request (ADR-0031); read-only, compute-never; clean not-found envelope |

There is **no unrestricted** KB search tool in V1. `kb_search` (1.9.0, ADR-0025)
is deliberately simple to *call* — a bare `{"query": ...}` is its entire request —
but it is **not** unrestricted: the server enforces a dual hard cap (call count
AND cumulative tokens returned) per (MCP session, authenticated subject), filters
every hit through the same team ACL as every other tool, and writes a
`retrieval_event` row per call. The old broker flow (`context.*`) remains
registered and optional — ADR-0025 keeps it available where citation-grade
provenance is required; `kb_search` is the preferred first stop.

## Request highlights

- `context.create_pack`: `run_id`, `task`, `approved_context_plan`,
  `retrieval_profile`, `budget_tokens ≥ 1`, optional `intent` (PR-33) ∈
  `how_does_x_work | why_was_x_changed | who_owns_x | what_calls_x`. `intent` is
  a **ranking hint only**: it drives the broker's transparent temporal
  re-weighting (below) and never changes ACL, version membership, or the L0
  verifier. Omitting it ⇒ neutral (pre-PR-33) ranking.
- `context.request_more` **requires** `context_pack_id`, `agent_name`,
  `question`, `why_needed`, `decision_needed`, `already_checked_evidence_ids`,
  `max_tokens`. A bare `{"query": ...}` fails schema validation.
- `context.open_evidence`: `context_pack_id`, `evidence_id`, `max_tokens`.
- `context.expand`: `seed_artifact_ids` (non-empty list of artifact UUIDs),
  `trust_floor` (default `EXTRACTED`) ∈ `EXTRACTED | INFERRED_HIGH | INFERRED_LOW`,
  `include_inferred` (default `false`), `budget_tokens ≥ 1`, optional
  `context_pack_id`. AMBIGUOUS and REJECTED are never returned (same admission
  rules as `graph.get_neighbors`). A bare `{"seed_artifact_ids": []}` fails
  schema validation (min_length=1).
- `context.create_change_pack`: `task` (non-empty), optional `target_hint`,
  `budget_tokens ≥ 1` (default 25000). Returns the curated file set to edit; it
  is a **file list, not bytes** (Postgres stays pointer-first — the runtime reads
  the files from its workspace). Resolution is deterministic and ranked: TARGET by
  exact symbol hint > exact file hint > lexical; TEST via `tests` edges / KB test
  artifacts / naming convention; DEPENDENCY via `imports`/`calls` edges, capped.
- `graph.get_neighbors`: `artifact_id`, optional `edge_types[]`, `depth` 1–3,
  `trust_floor` (default `EXTRACTED`), `include_inferred` (default `false`).
- `context.verify_answer`: `answer_id`, `claims[]` (each `claim_id`, `text`,
  non-empty `evidence_ids[]`), `graph_version` (null ⇒ active),
  `verifier_levels` (phase 1: `["L0"]`). A request with no claims, or any claim
  with empty `evidence_ids`, fails schema validation. Full shape and the L0
  check semantics live in `verification-receipt.md`.
- `kb_search`: `query` (non-empty). That is the whole request — no run/pack
  handle, no justification fields (this tool is the ADR-0025 *simple* path; the
  justified path stays `context.request_more`). Identity and budget bind to the
  authenticated session, never to a request field.
- `get_task_context`: `task_description` (non-empty), optional
  `hints {file_paths[], symbols[]}`, optional `confidence_floor` ∈
  `ground_truth | deterministic | interpreted` (default `interpreted` — admit
  everything), optional `max_tokens ≥ 1` (default: the server's Evidence-Pack
  cap; the request value is clamped to that cap, never an escape hatch). No
  run/pack handle; identity binds to the authenticated session. Resolution
  order is hints → alias index (`alias_reference` artifacts, PR-38 — the tool
  degrades gracefully to plain search when the KB predates PR-38 or an alias
  row's `body_text` JSON is unparseable) → keyword search fallback.
- `get_review_draft`: `repo` (`^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$` — an `owner/name`
  slug; the charset guard is the same log-injection discipline as `run_id`),
  `pr_number ≥ 1`, optional `head_sha` (same charset as `run_id`; omitted ⇒ the
  newest stored draft for `(repo, pr_number)` — this tool never calls GitHub to
  resolve a PR's *current* head SHA, unlike the review-panel CLI). No run/pack
  handle; identity binds to the authenticated session.
- `run_id` matches `^[A-Za-z0-9._-]{1,128}$` (log-injection guard).
- `context.read_pack.role` is **free-form** — adopting teams name their own
  roles (`security_auditor` is as valid as `implementation`); the broker never
  branches on it. It matches `^[A-Za-z0-9._-]{1,64}$` because the value lands
  verbatim in `key=value` audit logs (same forgery guard as `run_id`).
- `agent_name` / `role` are correlation/view fields only; identity binds to the
  authenticated session (Entra ID bearer token), never to the request body.

## Response highlights

- `context.request_more.status` ∈ `reused | approved | denied | needs_human_approval`;
  `status="denied"` requires `denial_reason`. Responses carry
  `tokens_returned` and `budget_remaining_tokens`.
- Status semantics (V1): `reused` = the question matched a previous retrieval
  (exact normalized match or semantic similarity ≥ threshold) and existing
  evidence is returned at no budget cost; `denied` = the requesting agent has
  exhausted its per-agent request count or token allowance; `needs_human_approval`
  = the request itself is justified but would exceed the remaining per-run
  budget — a human can raise the budget; `approved` = new evidence retrieved
  and charged.
- `evidence_id` is the artifact UUID rendered as a string in V1 — stable
  within a pack and storable in the ledger's UUID-array columns.
- Unknown `context_pack_id` or `evidence_id` ⇒ tool error plus a ledger row
  with the ledger-only status `error` (the broker holds pack state in memory
  per instance in V1; the durable record is the ledger). The same applies when
  no active `kb_version` exists. `error` rows use the sentinel `"-"` for
  `run_id`/`kb_version` values the broker could not resolve; `graph.get_neighbors`
  rows also use `run_id = "-"` because graph lookups are not run-scoped.
- `context.read_pack` charges no budget (reuse is the point); `create_pack`,
  `request_more`, and `open_evidence` charge the budgets they consume.
  `open_evidence` enforces both the per-run budget **and** the per-agent token
  allowance; exceeding either ⇒ tool error plus a `denied` ledger row.
- Per-agent allowances are keyed by the **authenticated session subject** and
  supplied per deployment via the optional `MCP_AGENT_ALLOWANCES` env var — a
  JSON object `{subject: {max_requests, max_tokens}}` (identifiers only, never
  secrets). Unlisted subjects get the conservative default (1 request / 4,000
  tokens). `max_requests: 0` is valid and means the subject may never
  `request_more`. Malformed config (bad JSON, padded or duplicate subject
  keys, non-integer values) fails the boot — it never silently defaults.
- `context.open_evidence` returns the raw text in `untrusted_content` plus
  `tokens_used`, `budget_remaining_tokens`, `source_uri`.
- `context.create_change_pack` returns `target_files`, `test_files`,
  `dependency_files` (each a `FileRef`: `path`, human `reason`, **numeric**
  `confidence` 0.0–1.0, `est_tokens`), `relevant_symbols[]`, and non-fatal
  `notes[]` (e.g. a test path proposed by naming convention for the runtime to
  verify). Writes one `retrieval_event` (`tool_name="context.create_change_pack"`,
  `status="approved"`, `run_id="-"` — selection is not run-scoped).
- Evidence card `title` and `summary` fields are derived from retrieved
  content: treat them as untrusted text, the same as `untrusted_content`.
- Every retrieval response (`create_pack`, `read_pack`, `request_more`,
  `open_evidence`, `get_neighbors`) carries an `authorization` decision:
  `{policy, decision}` where `policy` names the active filter (V1:
  `team_acl_v1`) and `decision` is always `allowed` — unauthorized artifacts
  are silently removed before ranking, and the response deliberately carries
  **no filtered count** (counts would leak the existence of restricted
  artifacts). A fully-denied `open_evidence` is a tool error
  (`evidence not available`) — the same error as a missing artifact or an id
  that was never in the pack, so none of the three is distinguishable.
- Evidence cards carry **temporal semantics** (PR-33, ADR-0010/0011 phase 4),
  derived deterministically (no LLM) from already-stored data: `source_kind` ∈
  `code | doc | card | pr | adr | other` (from `source_type` + `artifact_type`),
  `temporal_state` ∈ `current | superseded` (from version-membership
  `invalidated_at_seq` + source `is_deleted`), and `stale_for_intent` (true when
  a doc references a removed/absent code symbol under a structure-seeking intent).
  When `create_pack.intent` (or `request_more`) is supplied, the broker
  **transparently re-weights** the candidate set — current code lifted for
  `how_does_x_work` / `what_calls_x`, cards/PRs/ADRs lifted for
  `why_was_x_changed`, ownership/recent commits for `who_owns_x`, superseded and
  stale docs downranked. The weighting is **logged** (`event=temporal_weight*`),
  **deterministic** (stable tie-break by `artifact_id`), and is a ranking/label
  signal ONLY: it never removes historical evidence (a `why` query still sees it),
  never promotes a contradicting doc into claim support, and is **independent of
  the L0 `not_stale` verifier check** — a doc downranked here still passes L0 if
  its source is in the active version.
- `context.expand` returns `{cards, tokens_used, truncated, authorization}`.
  `truncated=true` when the budget cap was hit and not all BFS results fit. The
  response carries an `authorization` decision. Each call writes one
  `retrieval_event` with `tool_name="context.expand"`, `status="approved"`,
  and the seed + expanded artifact ids. If `context_pack_id` is given, the
  pack's run budget is charged (`pack.charge`) and new cards are inserted into
  the pack (openable by handle via `context.open_evidence`). Seeds already in
  the pack are de-duplicated from the returned cards. Budget clamped to
  `max_run_budget_tokens` and, when a pack is supplied, to the pack's
  `run_remaining_tokens`.
- `graph.get_neighbors` is **trust-aware** (ADR-0011, `trust-buckets.md`).
  Each returned `GraphNeighbor` carries the connecting edge's `trust_class` and
  a `claim_supporting` flag (true only for `EXTRACTED`). `trust_floor` defaults
  to `EXTRACTED`, so the default result is exactly the directly-extracted graph.
  `AMBIGUOUS` and `REJECTED` edges (and any unknown/banned bucket, treated as
  `AMBIGUOUS`) are **never** returned and never transited. `INFERRED_HIGH` /
  `INFERRED_LOW` edges are returned **only** when `include_inferred=true`, with
  `claim_supporting=false` — they are routing hints to source evidence, never
  themselves the cited support for a platform-trusted claim. Trust filtering is
  applied to every edge before the frontier expands and **composes with the ACL
  filter**: an edge must clear both trust admission and per-hop authorization.
  (scanned over title + summary); `open_evidence` responses carry the same pair
  scanned over the expanded body. Flagging is advisory and deterministic
  (regex, no model calls); flagged content is returned **verbatim**, never
  rewritten — the consumer decides how to treat it, the broker never lets it
  alter policy.
- `context.verify_answer` returns a **verification receipt**
  (`verification-receipt.md`): `receipt_schema_version=1`, `answer_hash`
  (sha256 over the normalized claims, stable for the same normalized input),
  `graph_version`, `issued_at`, `verifier_levels_run=["L0"]`, `overall ∈
  {passed, failed, partial}`, and `claim_results[]` (per-claim `result`,
  the six `L0_*` `checks`, and `failed_reasons[]`). A claim passes L0 iff every
  cited evidence id exists, is in the served `graph_version`, is ACL-visible to
  the requester, appears in the requester's retrieval ledger, is not stale
  (its source not superseded/deleted), and is supported by an `EXTRACTED`
  edge (an `INFERRED_*` routing hint cannot be the sole support). `overall` is
  `passed` iff all claims passed, `failed` iff all failed, else `partial`.
  The verifier stamps the **validated** `client_id` (the authenticated client
  identity, never a request field) into the receipt and **binds it into the
  signed payload**, so a receipt is scoped to the client it was issued to — a
  valid receipt for client A does NOT validate for client B. `signature` /
  `key_id` are populated when a signing key is configured (PR-31). The verifier
  performs **no generation**; every call writes a `retrieval_event` logging
  ids/hashes/outcomes only — never answer or evidence text.
- `context.platform_trust` is the **official-client gate** (ADR-0011 §6). It
  takes an optional `receipt` (the one the client got from
  `context.verify_answer`) and returns a `PlatformTrustDecision`
  (`status ∈ {trusted, denied, not_required}`, `client_id`,
  `verification_required`, `reason`). The calling client's identity comes from
  the authenticated session. For a client whose registry policy sets
  `verification_required`, `status` is `trusted` **only** with a valid,
  client-matched, **passing** receipt; otherwise `denied` with a stable `reason`
  (`verification_required_no_receipt`, `receipt_unsigned`,
  `receipt_client_mismatch`, `receipt_signature_invalid`,
  `receipt_overall_not_passed`) — never a silent pass. A client that did **not**
  opt into `verification_required` gets `not_required` (its behaviour is
  unchanged).
- `kb_search` returns `{results, budget_remaining, notice}`. Each hit carries
  `title`, `artifact_type`, `source_uri`, `snippet` (untrusted retrieved text,
  same discipline as card titles/summaries), and a `confidence_tier` ∈
  `ground_truth | deterministic | interpreted`
  (`docs/proposals/2026-07-02-tool-design-first-kb-architecture.md` §3).
  `snippet` is built from the artifact's `body_text` **else** its
  `search_text` (1.12.0, ADR-0033): a pointer-only `code_file` row
  (`body_text=NULL`) serves the build-time deterministic code skeleton stored
  in its `search_text` instead of an empty snippet. Skeleton text is
  display/search material — for *thinking*, never *citing*; citations still
  resolve through evidence ids to the raw `body_text` / source pointer.
  **Determinism (1.12.0):** `results` are in deterministic rank order (stable
  `artifact_id` tie-break); field order is fixed with `results` first;
  `budget_remaining` + `notice` are the documented **volatile tail** (they
  reflect prior usage of the caller's budget window, so two identical requests
  are byte-identical up to that tail).
  Keyword-ranked hits are always `interpreted` (relevance-ranked, not
  cross-validated); the field is the declared extension point for graph-derived
  hits to carry `deterministic` once blast-radius wiring lands (follow-up PR —
  no other tier is emitted today). `budget_remaining = {calls, tokens}` states
  what is left of the caller's dual cap after this call (floored at 0). When the
  budget closes, `notice` carries exactly: *"KB budget spent — work with what
  you have, or read the specific files you still need."* — appended to the last
  in-budget response and returned (with empty `results`, ledger `status="denied"`,
  **never a tool error**) for every call after it, so the agent keeps working
  with files instead of crashing (ADR-0025 §4). Hits are ranked by the standard
  retrieval path (SearchClient relevance × transparent temporal/centrality
  factors, semantic dedupe, 3–5 results max) and ledgered with
  `run_id = "-"` (the request carries no run handle; the session is recorded in
  `details`).
- `get_task_context` returns `{resolved_scope, referenced_paths, blast_radius,
  conventions, similar_prior_changes, evidence_ids, open_questions,
  budget_used}` — in exactly that field order: stable identifiers first,
  `budget_used` last as the documented **volatile tail**
  (`docs/proposals/2026-07-02-tool-design-first-kb-architecture.md` §2,
  path dedup + stability per ADR-0033 / 1.12.0):
  - `resolved_scope.entities[]`: `{entity_id, path, symbol, resolution_source ∈
    alias_index | hint | search, confidence_tier}`. When resolution is genuinely
    ambiguous (a hint or alias matches several distinct targets with no clear
    winner), the tool returns `resolved_scope.ambiguous_candidates[]`
    (`{alias_text, candidates[], reason}`) plus an `open_questions` entry and
    NO guessed entity — an ambiguous answer is an answer, so it does not
    trigger the broadened retry (only a truly empty scope does, once).
  - `referenced_paths[]`: the canonical **path table** — every path any
    blast-radius entry surfaces, deduplicated, sorted lexicographically. A
    path string appears here once in full; blast entries reference it by
    index. Scope entities keep their full `path` (the primary answer, ≤5
    entries); `similar_prior_changes` entries carry commit ids, never file
    paths, so they reference nothing.
  - `blast_radius.{callers,callees,tests}[]`: `{entity_id, path_ref, symbol,
    edge_type, confidence_tier, caveat}` where `path_ref` is the entry's index
    into `referenced_paths` (dedup: the full path string is never repeated
    per entry). Traversal covers `calls` / `imports` /
    `tests` edges from the resolved entities, EXTRACTED trust class only.
    **Confidence rule (2026-07-02 Graphify audit):** a `calls` edge is
    `deterministic` ONLY if the caller and target are defined in the same file
    OR the caller's file has an `imports` edge to the target's file; anything
    short of that (including a missing `defined_in` for either side) is
    `interpreted` with a non-null `caveat` naming the missing corroboration.
    `imports` and `tests` edges are direct AST facts and stay `deterministic`.
  - `conventions[]`: `{pattern, evidence_ids, confidence_tier}` — rule/ADR/doc
    artifacts relevant to the resolved scope's directories; always
    `interpreted` in v1.
  - `similar_prior_changes[]`: `{commit_or_pr_id, summary, evidence_ids}` —
    keyword hits over `commit` artifacts (`interpreted`-class content).
  - `confidence_floor` filters the response: a floor above `interpreted`
    removes interpreted-tier entities/edges and empties `conventions` +
    `similar_prior_changes` rather than silently blending them in (drops are
    logged, `event=task_context_floor_filtered`).
  - `evidence_ids` = every artifact id cited anywhere in the response;
    `budget_used = {tokens, calls}` where `tokens` is the estimate of the EXACT
    serialized response (meter == wire, the kb_search rule) and `calls` counts
    the internal retrieval operations the backend ran.
  - **Determinism (1.12.0):** every list has a documented sort —
    `resolved_scope.entities` in resolution order (hint order, then alias
    target rank, then search rank with an `artifact_id` tie-break);
    `ambiguous_candidates` in hint/alias encounter order; `referenced_paths`
    lexicographic; each `blast_radius` bucket by `(path, symbol, entity_id)`;
    `conventions` and `similar_prior_changes` in search-rank order;
    `evidence_ids` sorted by UUID string; `open_questions` in generation
    order. Two identical requests against the same served build produce
    byte-identical JSON (the volatile tail, `budget_used`, is itself
    deterministic here — it derives from the serialized response and the
    internal call count, not wall-clock state).
  - Budget: the serialized response is capped server-side at the Evidence-Pack
    band (`task_context_max_tokens`, default 8k — the top of the 6k–8k band).
    Over-budget responses are trimmed deterministically from the lowest-value
    tail (`similar_prior_changes` → `conventions` → `callees` → `callers` →
    `tests`; never the resolved scope, ambiguity, or open questions), logged as
    `event=task_context_budget_trim`.
  - Backend structure (ADR-0030 Decision §2): a LangGraph StateGraph — four
    genuinely parallel pure-retrieval nodes (`resolve_scope`, `blast_radius`,
    `conventions`, `similar_prior_changes`) joined by a `synthesize` node, with
    ONE conditional broadened retry when scope resolves empty, then an honest
    answer with what is known. Zero LLM calls at query time. Per-step tracing
    (ADR-0032) emits one root span plus one span per node to Postgres via the
    `TraceSink` port (`TRACE_SINK=postgres|none`, default `postgres`) — fail-soft
    always, no budget charge. LangChain's native `LANGSMITH_*` env instrumentation
    remains inert and is not part of the tracing story.
  - Ledger + ACL: one `retrieval_event` per call (`tool_name="get_task_context"`,
    `run_id="-"`, `returned_artifact_ids` = the evidence ids, `tokens_returned` =
    `budget_used.tokens`; ledger-only `error` status when no active kb_version).
    Every artifact surfaced anywhere in the response is hydrated from Postgres
    and filtered by the requester's team ACL first; an unauthorized neighbor is
    dropped before it can reveal its connectivity (same rule as
    `graph.get_neighbors`), and suppressed ids are audit-logged.
- `get_review_draft` returns `{found, draft}`. `found=false` ⇒ `draft` is
  `null` — the clean not-found envelope (no draft computed yet for that PR),
  never a tool error. `found=true` ⇒ `draft` is the stored row intact:
  `{draft_key, repo, pr_number, head_sha, created_at, draft}`, where the inner
  `draft` is the review-panel-owned `review_draft_v1` JSON document
  (`docs/contracts/review-panel.md`) passed through **verbatim** — mcp-server
  does not parse, validate, or reshape it (that schema is review-panel's, kept
  in sync only through the contract, never by import, ADR-0008). Treat every
  field inside it as untrusted retrieved content, the same discipline as every
  other card/snippet field in this contract. Read-only: the tool `SELECT`s the
  `review_panel.review_draft` table review-panel owns (cross-schema READ, same
  posture as this server already reading the kb-builder-owned Knowledge
  Registry) over mcp-server's own `DATABASE_URL` connection — it never writes
  to `review_panel` or any registry table, and never calls a model or
  triggers computation. **Known limitation:** this only works when
  `DATABASE_URL` and `REVIEW_PANEL_DATABASE_URL` point at the same Postgres
  database (`docs/dev-guide/reference/database.md`, on the `review_panel`
  schema, already flags they are not always the same); when the `review_panel`
  schema/table is absent (a different database, or review-panel has never run)
  the call fails as an unexpected error, ledgered `status="error"` — distinct
  from the clean `found=false` "no draft yet" case. Every call writes exactly
  one `retrieval_event` row (`tool_name="get_review_draft"`, `run_id="-"`,
  `kb_version="-"` — this tool is not Knowledge-Registry-scoped,
  `status="approved"` whether found or not, `status="error"` only on an
  unexpected failure). Carries **no** budget charge: `kb_search`'s
  call/token allowance is untouched by this tool (fetching a stored draft is
  not knowledge retrieval). Visible to any authenticated requester in v1
  (single-team local scope) — a multi-team ACL over drafts (mirroring
  `team_acl_v1` on registry artifacts) is a recorded layer-2 item, not solved
  here.
- `ledger.list_retrievals` returns one record per retrieval event:
  `event_id`, `run_id`, `kb_version`, `agent_name`, `tool`, `status`,
  `cache_hit`, `tokens_returned`, `evidence_ids`, `created_at`. The non-run
  sentinel `run_id = "-"` is rejected (it aggregates every subject's
  non-run-scoped activity and is operator-only). **Results are subject-scoped:**
  the listing is filtered to the requesting authenticated session subject's own
  events (`agent_name = requester.subject`), so a `run_id` is not a grant to read
  a co-agent's returned/reused/new evidence UUIDs or token spend (invariant 6).
  A run-owner/orchestrator view spanning every subject of its run is a recorded
  follow-up (an operator/run-owner role), not a V1 default.

## Server-side policy (not prompt-enforced)

- Per-run and per-agent budgets enforced in the broker; the run budget
  requested by `create_pack` is clamped to a server-side maximum (default
  18k, the top of the 12k–18k band) — the request value is never an escape
  hatch; reuse before retrieve;
  semantic dedupe (duplicate threshold starts at 0.88–0.92 and is tuned from
  ledger logs — see `.claude/rules/token-budgets.md`); 3–5 cards max per
  retrieval after rerank. Per-agent identity binds to the authenticated
  session subject, never to `agent_name`.
- **Within-retrieval dedupe runs on every retrieval path** (not just
  cross-query reuse): after rerank and **before** the card cap, near-duplicate
  candidates (normalized title+summary similarity ≥ the configured
  `semantic_dupe_threshold`, default 0.90) collapse to the higher-ranked one, so
  two artifacts that surface as the same card never each consume a card slot.
  Dropped ids are logged (`event=retrieval_deduped`). Deterministic
  (token-similarity, no model calls; stable rank-order tie-break).
- **`create_pack` is never born over its run budget.** After dedupe + the card
  cap, if the surviving cards' tokens exceed the (clamped) `budget_tokens`, the
  broker trims the lowest-ranked cards until they fit (logged
  `event=create_pack_budget_trim`) and reports the trimmed `budget_used_tokens`.
- **The per-agent follow-up allowance is run-scoped, not pack-scoped.** Re-creating
  the pack within a run (e.g. against a newly active `kb_version`) reuses the same
  per-(run, subject) usage counters, so an agent cannot reset its `request_more`
  allowance by re-creating the pack. `create_pack` itself remains free of the
  follow-up request/token meter (that meter governs `request_more` /
  `open_evidence`); only its run-budget trim applies.
- **`kb_search` enforces ADR-0025 §4's one restriction: a dual hard cap, in code.**
  The caller's `AgentAllowance` (`MCP_AGENT_ALLOWANCES`, keyed by authenticated
  subject — the same allowance map the `context.*` meter uses; unlisted subjects
  get the conservative default) is enforced on **both axes independently**: the
  tool answers only while `requests_used < max_requests` **and**
  `tokens_used < max_tokens`. One axis without the other is a bug — exhausting
  either closes the budget. Tokens are charged **after** each answer for the
  exact serialized hits returned, so the final in-budget call may overdraw the
  token cap; the next call is then refused. The budget window is the pair
  (MCP session id, subject) — one agent run/connection = one task budget; a new
  session gets a fresh window, and windows are held in bounded process memory
  (V1 single instance, like pack state — the durable record is the ledger).
  **Known limit (2026-07-03 architecture review):** because the window is
  per-connection, "per-task" is enforceable only as "per-session" — a client
  that reconnects mints a fresh budget. A host-signaled task boundary or TTL
  would need a request-schema extension and is deliberately deferred until
  real usage data exists.
  Check-then-charge is serialized per window, so a parallel burst of `kb_search`
  calls cannot all pass the cap before any of them charges.
- The broker makes **no LLM or embedding calls** in V1: pack summaries are
  assembled from registry artifacts, and semantic dedupe is a deterministic
  token-similarity measure. Retrieval relevance goes through the `SearchClient`
  interface (Postgres keyword implementation locally; the Azure AI Search
  implementation stays behind the same interface).
- **The ledger is complete by construction, including crashes and
  schema-rejected calls.** Every call writes exactly one `retrieval_event` row
  — `approved`, `denied`, or `error` (see `postgres-knowledge-registry.md`) —
  even when the call fails unexpectedly mid-flight, or never reaches a handler
  at all. A uniform tool wrapper (`mcp/tool_handlers.py`) ledgers any exception
  a handler has not already ledgered itself (a `LedgeredToolError` marks the
  ones that have, so no call is ever double-ledgered), and refunds any budget
  charge made before the crash (e.g. `kb_search`'s call/token counters,
  restored under the same window lock the charge used; the pack-scoped tools —
  `context.open_evidence`, `context.expand`, `context.request_more` — refund
  the pack's run/agent token counters, and `request_more` also its
  dedupe-history entry and new cards, all restored under the same
  `EvidencePackState.lock` acquisition as the charge via
  `EvidencePackState.snapshot`/`restore`). A failing platform never silently
  vanishes from the ledger or eats an agent's budget, and the exception always
  still reaches the caller. If the error-ledger write itself fails (the
  database is fully down), the original exception still surfaces — never
  masked by the ledger failure — and the ledger-write failure is logged with
  structured fields.
  A call whose arguments fail the tool's request schema is a distinct case:
  fastmcp validates arguments before invoking the registered callable, so the
  rejection happens BEFORE `mcp/tool_handlers.py` ever runs and the uniform
  wrapper above cannot see it. `SchemaRejectionLedgerMiddleware`
  (`mcp/schema_rejection_middleware.py`) is the MCP-boundary counterpart: it
  wraps the whole call (validation included), writes one `error` row on a
  `pydantic.ValidationError` (`details` carries the exception type and a terse
  field/type/message validation summary — NEVER the raw argument values a host
  sent), and re-raises the same validation error unchanged so the host still
  gets the schema feedback its retry loop depends on. No handler ever ran, so
  no budget was ever charged — there is nothing to refund. Same fail-soft
  discipline: a ledger-write failure here is logged and swallowed, never
  masking the validation error.
- Results are filtered by the requester's authorization before returning
  (PR-13: `team_acl_v1`). The requester is the authenticated session subject
  plus its team set, taken from the bearer token's `groups`/`roles` claims —
  never from the request body. An artifact with empty `acl_teams` is
  org-public (any authenticated subject); a non-empty `acl_teams` requires a
  non-empty intersection with the requester's teams. Filtering applies at
  every surface: card retrieval, pack reads (`read_pack` re-filters the
  cached cards against the reading requester), reuse (`request_more` reused
  ids are re-filtered for the caller; a fully-suppressed reuse falls through
  to a fresh, filtered retrieval), evidence expansion
  (`open_evidence` re-hydrates from Postgres and re-filters — a pack handle
  is not a grant), and graph traversal, where the root node and each BFS hop
  are filtered **before** expanding the frontier so restricted nodes never
  reveal their connectivity — an unauthorized root returns the same empty
  result as an unknown id.
- Retrieval, graph traversal, and provenance filter by **version membership**,
  not `kb_version` label-equality (`version-membership.md`, ADR-0013). The broker
  resolves the active build's `build_seq` once (from `kb_build_run WHERE
  status='active'`) and serves every artifact / edge / provenance / neighbour /
  search row where `valid_from_seq <= S AND (invalidated_at_seq IS NULL OR
  invalidated_at_seq > S)` for that served `S`. An artifact introduced by an
  earlier build is still served by a later active version; an artifact
  invalidated in the active build is no longer served by it but is still served
  by the prior version. `kb_version` remains on the row and in responses as a
  label only.
- Retrieved text is untrusted and cannot alter tool policy or instructions.
  The broker scans retrieved text for injection patterns (instruction
  overrides, role markers, chat-template tokens, secret-exfiltration asks,
  unicode direction/zero-width tricks) and marks matches via the
  `injection_*` response fields; detections are audit-logged.
- Every context expansion and source access is audit-logged to structured
  stdout (`telemetry/audit.py`): requester subject + teams, tool, artifact
  ids, ACL-suppressed artifact ids, and injection detections. Audit lines
  carry ids and metadata only — never `body_text`. The audit stream is
  operator telemetry; the Postgres `retrieval_event` ledger remains the
  agent-visible durable record.
- Unauthenticated requests are rejected at the transport (401) and never reach
  a tool. `/health` is the only unauthenticated route and discloses nothing but
  service name and active `kb_version`.
- A request carries BOTH the per-user subject (`Requester`) **and** a registered
  **client/app identity** (`ClientIdentity`: `client_id`, `scopes`,
  `verification_required`), resolved in the auth layer from the authenticated
  client credential (the bearer token's `client_id`) — never a request field.
  The client registry is config-driven via the optional `MCP_CLIENT_REGISTRY`
  env var: a JSON object `{client_id: {scopes?: [str],
  verification_required?: bool, secret_env?: str}}`. It carries **identifiers +
  policy only** — any client secret is referenced by env/Key Vault **NAME**
  (`secret_env`), never a value; a value-shaped key (`secret`, `client_secret`,
  `key`, `password`, `credential`) fails the boot. Malformed config fails the
  boot (it never silently grants/denies). A client **absent** from the registry
  resolves to an unregistered identity (no scopes, `verification_required=false`)
  — deployments that ship no registry are unchanged, and verification is **never**
  made mandatory for a non-opted-in client.
- Client **scopes** gate the tool surface **additively**: a registered client
  must hold a tool's required scope (`context.read`, `graph.read`, `ledger.read`,
  `context.verify`) or the call is denied before the broker runs. This composes
  WITH (it never replaces or widens) the per-team user ACL — defence in depth.
  An unregistered client is never scope-gated (opt-in only).

## Versioning

Any breaking change bumps `MCP_SCHEMA_VERSION`, updates this document in the
same PR, and is validated by the contract tests in
`services/mcp-server/tests/contract/`.
