# Proposal — Tool-Design-First KB Architecture

## Status

Proposal, not an ADR. Requires an ADR before any part of this is adopted for real (per
`CLAUDE.md` — "stop and write an ADR proposing it" for any notable design deviation). This
document is Task 3 of the 2026-07-02 ground-up-rebuild analysis; Task 4
(`2026-07-02-phased-build-plan.md`) is the execution plan for validating it.

## Premise

We do not control the host agent loops (Copilot, OpenCode, GitHub Copilot). Any orchestration we
build only applies to processes we fully own: the nightly build-time pipeline, and a local/CI
review-and-PR agent. Our entire leverage over what a *host-served* coding session does is therefore
in two places only:

1. **What we precompute at build time**, and
2. **How good a single tool-call response is** — because a host agent has no reason to fall back
   to raw file reading if one call already gave it what it needed.

Everything below is designed against that constraint, not against "how do we get more control of
the loop" — we can't.

## What carries forward from the current architecture, unchanged

This proposal is a rethink of the *retrieval and tool-design* layer, not of the storage or
governance layer. Nothing here overrides the existing non-negotiable invariants in `CLAUDE.md`:

- **Postgres stays the source of truth.** Every artifact type introduced below is a Postgres row
  with a `content_hash`, `kb_version`, and `acl_teams`, exactly like existing artifact types. No
  new artifact bypasses the ledger, the ACL filter, or the incremental-build content-hash gate.
- **A KB version goes active only after validation passes** (invariant 5) — applies to the new
  artifact types the same as the existing ones.
- **Third-party extraction output is never trusted as our trust class.** This is not a new rule —
  it's ADR-0012's rule, generalized. See Confidence Tiering below for why this proposal leans on it
  even harder than the current system does.

## 1. Alias / Reference Index

**Purpose.** Let a developer type "the login bug" or "the retrieval budget check" and have it
resolve to the right file/function/class without writing a paragraph of context. This is the
mechanism that makes a terse host-agent prompt viable at all.

**Artifact type: `alias_reference`**

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | PK |
| `alias_text` | text | Normalized informal phrase ("login bug", "retrieval budget check") |
| `alias_text_embedding` | vector | For fuzzy/semantic lookup, not just substring match |
| `target_entity_ids` | uuid[] | FK(s) into `knowledge_artifact` — usually 1, can be >1 if the phrase legitimately spans several files |
| `confidence_tier` | enum | See §3 — starts at `interpreted`, promotes to `deterministic` after confirmation (see §4) |
| `source_evidence_ids` | uuid[] | Commit SHA / PR number / ticket ID artifact(s) that established this mapping |
| `confirmation_count` | int | Incremented by the feedback loop; never decremented, only superseded |
| `kb_version` | uuid | Standard versioning |
| `acl_teams` | text[] | Inherited from the source evidence, same rule as `git_metadata` (intersection of touched files' ACLs) |

**Mining sources (build-time, LLM-interpreted tier at creation).** Commit messages, PR
titles/descriptions, and ticket titles (the `ado_card` connector already ingests these) are the
raw material. This is a **derived artifact built from existing connector output**, not a new
connector — it reuses the `git_metadata` commit artifacts and `ado_card` artifacts that already
exist, and reuses the incremental-build discipline (a commit already has a `content_hash`; if it's
unchanged, its alias-mining pass is skipped, exactly like docify/graphify skip on cache hit).

Extraction prompt per new commit/PR/ticket artifact since the last build: *given this commit
message/PR title and the files it touched, what informal name(s) would a developer plausibly use to
refer to this change, and which of the touched files/symbols does each name point to?* Output is
zero or more `alias_reference` candidates at `interpreted` tier.

**Resolution at query time.** Embedding lookup against `alias_text_embedding` first (cheap,
precomputed, no LLM call). On a clean single match above a similarity floor, return it. On no
match or multiple close matches, `get_task_context` (below) falls through to a broader search and
returns the ambiguity explicitly rather than guessing — see §3 and §4.

## 2. `get_task_context(task_description)` tool

One call, so the host agent has no reason to fall back to raw file reading.

**Input schema**

```json
{
  "task_description": "string, required — the developer's terse or detailed task description",
  "hints": {
    "file_paths": ["string, optional — paths the developer already mentioned"],
    "symbols": ["string, optional — symbol names already mentioned"]
  },
  "confidence_floor": "ground_truth | deterministic | interpreted, optional, default: interpreted",
  "max_tokens": "integer, optional, default: the mcp-server's standard Evidence Pack budget (6k-8k per token-budgets.md)"
}
```

**Output schema**

```json
{
  "resolved_scope": {
    "entities": [
      { "entity_id": "uuid", "path": "string", "symbol": "string|null",
        "resolution_source": "alias_index | hint | search",
        "confidence_tier": "ground_truth | deterministic | interpreted" }
    ],
    "ambiguous_candidates": [
      { "alias_text": "string", "candidates": ["entity_id", "..."],
        "reason": "string — why resolution stopped short of a single answer" }
    ]
  },
  "blast_radius": {
    "callers": [ { "entity_id": "uuid", "confidence_tier": "...", "caveat": "string|null" } ],
    "callees": [ { "entity_id": "uuid", "confidence_tier": "...", "caveat": "string|null" } ],
    "tests": [ { "entity_id": "uuid", "confidence_tier": "..." } ]
  },
  "conventions": [
    { "pattern": "string", "evidence_ids": ["uuid"], "confidence_tier": "interpreted" }
  ],
  "similar_prior_changes": [
    { "commit_or_pr_id": "string", "summary": "string", "evidence_ids": ["uuid"] }
  ],
  "evidence_ids": ["uuid — every artifact id cited above, for the retrieval ledger"],
  "budget_used": { "tokens": "integer", "calls": "integer" },
  "open_questions": ["string — populated when resolution or blast radius is genuinely uncertain; never silently guessed"]
}
```

Every claim in this response carries an evidence ID (invariant 7) and a confidence tier. If
`resolved_scope.entities` is empty and `ambiguous_candidates` is non-empty, the tool is explicitly
telling the caller "I don't know, here are the candidates" rather than picking one.

## 3. Confidence tiering

Three tiers, carried as a field on every artifact this system produces or serves:

| Tier | What it is | Trust model |
|---|---|---|
| `ground_truth` | Raw source bytes at a specific commit SHA | Always correct by construction; never stale relative to what's cited (it *is* the cited thing) |
| `deterministic` | Graphify's AST-derived structure — symbols, containment, **name-unambiguous** calls | Machine-derived, no LLM, but not infallible — see below |
| `interpreted` | LLM summaries, pattern-mined conventions, alias-index entries not yet confirmed | Useful for recall, must never be the sole basis for a high-stakes claim |

**Why `deterministic` needs a caveat, grounded in a concrete finding, not theory.** ADR-0012
already knew Graphify's `calls` edges can't be trusted verbatim — its adapter drops any call site
that resolves to more than one target, because a syntactic name match isn't a resolved semantic
call. During this analysis we re-ran that exact test class against the currently-pinned Graphify
version (`graphifyy==0.8.39`) with a fresh fixture (a free function and a same-named class method,
a caller of the free function) and found a **second failure shape the existing adapter doesn't
catch**: the call resolved to a **single, confidently-labeled `EXTRACTED` target — and it was the
wrong one.** The correct target got zero incoming edges. This isn't an ambiguous multi-target case;
it's a single wrong answer presented with full confidence.

**Rule for this proposal:** a Graphify `calls` edge is `deterministic` tier only if (a) the call
site has no name-collision candidates anywhere in the analyzed scope, **and** (b) the target's
identity is corroborated by at least one independent signal (e.g., the import graph explicitly
names the target's module, not just a bare identifier match). Anything short of that is
`interpreted` tier and must surface as a `caveat` string in `blast_radius`, never presented as flat
fact. This is a direct extension of ADR-0012's existing philosophy ("re-derive trust, never copy
Graphify's label"), not a new one — it closes a gap the June spike didn't have visibility into.

`confidence_floor` on the tool input lets a caller doing something high-stakes (e.g. an automated
PR decision) ask for `deterministic`-or-better only, forcing `interpreted`-tier content out of the
response rather than silently blending it in.

## 4. Feedback loop

We can't observe what happens inside a host agent's own clarifying-question exchange with the
developer — that loop is opaque to us (same constraint as everywhere else in this document). Two
mechanisms that work *without* needing host-loop visibility:

1. **Explicit, cheap tool call.** A `confirm_alias(alias_text, entity_id)` tool the host agent can
   call once it and the developer have settled an ambiguity. This is just another MCP tool call —
   it requires nothing from the host runtime beyond what it already does (call a tool when it has a
   reason to). Confirmed aliases get `confidence_tier: deterministic` and their
   `confirmation_count` incremented.
2. **Passive nightly inference.** If a landed commit or PR is linked (via ticket ID or PR
   description) to an `alias_reference` that was previously `ambiguous_candidates` in some past
   `get_task_context` call recorded in the retrieval ledger, the nightly build can retroactively
   check which files the commit actually touched and promote the matching candidate — no
   confirmation call needed, works purely from data we already own.

Both mechanisms are things we can define as MCP tools or nightly-build steps, not host-runtime
orchestration — consistent with the constraint stated in the premise.

## Open questions (not resolved by this document)

- Exact similarity floor for alias-index fuzzy match — needs tuning against real data (Task 4,
  Phase 1).
- Whether "similar prior changes" should reuse `evals/agent_task_cases` infrastructure or needs its
  own commit-similarity index — deferred to Phase 2 implementation.
- Whether the corroboration signal for promoting a Graphify call edge to `deterministic` (import
  graph cross-check) is sufficient, or needs a second independent signal — this is itself an
  empirical question, not a design one; Task 4 Phase 1 includes a dedicated adversarial test for it.
