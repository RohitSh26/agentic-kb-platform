# ADR-0013 — kb_version membership model + identity-over-time

## Status

**Accepted** (2026-06-14) — the platform owner directed implementation of this design ("complete the
new design we discussed"); PR-27 implements it. This ADR **assumes** the production cadence is
incremental nightly (per ADR-0004), which makes §1 (interval membership) mandatory; the cadence
question in "Open question" below is not yet explicitly answered — if production does full nightly
rebuilds instead, §1 reduces to a deletion/rename sweep.

## Context

PR-27 (identity-over-time invalidation) surfaced a latent contradiction between ADR-0004 (nightly
**incremental** build — "skip unchanged content by content hash … MCP always serves the last
successful active version") and the implementation. Today:

- Every `knowledge_artifact` / `knowledge_edge` is stamped with one `kb_version` **at creation and
  never re-stamped**. On a `generation_cache` hit the build returns the cached rows unchanged
  (`build_runner.py` `_wikify_gated`/`_graphify_gated`), so they keep the label of the build that
  first created them.
- `build` generates a **per-build** `kb_version` (`local.<timestamp>`).
- mcp-server retrieval scopes **strictly** `WHERE kb_version = :active`
  (`infrastructure/postgres/artifacts.py`, `edges.py`, `provenance.py`).

**The bug:** an incremental build `N` re-creates only the *changed* artifacts (label `N`); the
thousands of unchanged artifacts keep labels `< N`. Activating `N` then makes MCP serve
`WHERE kb_version = N` — i.e. **only that day's delta**, with everything unchanged invisible. The
first full build works (one label); every incremental build afterward silently shrinks the served
KB. This directly defeats ADR-0004's "serve the last active version" and invariant 5.

A version is currently defined as "rows created in this build", but it **must** mean "the complete
set of artifacts/edges valid as of this build". Until that is fixed, PR-27 cannot define what
deletion "removes from the new version", what a rename "reattaches to", or what a "ghost edge" is —
and the `no-ghost-edges` publish gate (`publish_gates.py` `edge_evidence_integrity_gate`, which
already counts endpoints "missing from THIS kb_version") would flag every legitimate cross-version
edge and block every incremental build.

A second, related gap: **artifact identity is path-bound.** A renamed file becomes a new
`source_item` → new artifacts → the old ones orphan and every edge to them dangles. There is no
stable identity or rename link. And an ACL-only change on a source never propagates to its existing
artifacts (`write.py` carries a `TODO(acl-propagation)` with a waiting test).

## Decision (proposed)

### 1. Version membership is a validity interval, not the creation label

Keep each artifact/edge row **immutable and stamped once** at creation. Add a validity interval keyed
to a monotonic **build sequence** (a new `kb_build_run.build_seq bigint`, assigned at run start):

- `valid_from_seq` — the build that introduced the row (set at creation).
- `invalidated_at_seq` — `NULL` while live; set to build `N` when the row leaves the KB in build `N`
  (deletion or rename-away). **Setting it does not mutate any past version** — the row remains a
  member of every version `< N`, so prior active versions stay byte-reconstructable (invariant 5).

A row is a **member of version `S`** iff `valid_from_seq ≤ S AND (invalidated_at_seq IS NULL OR
invalidated_at_seq > S)`. MCP resolves the active build's `build_seq` and serves by interval instead
of `= kb_version`. Incremental builds re-LLM nothing (the generation cache still gates every model
call, invariant 4); they only flip `invalidated_at_seq` on the few rows that actually left.

> Rejected alternative — **re-stamp survivors to `N` in place**: simplest, but UPDATEing rows the
> previous active version is still serving mutates a published version (forbidden, invariant 5 / PR-27
> "Do NOT mutate a published kb_version in place"). Rejected.
>
> Rejected alternative — **full copy per version**: immutable but duplicates the whole registry every
> night; defeats ADR-0004's "cost scales with change, not corpus size". Rejected.
>
> Rejected alternative — **stable reused `kb_version` label + refresh-in-place**: makes "immutable
> version" a fiction; no rollback to a prior version. Rejected.

### 2. Stable artifact identity + rename link

Identity is `(source_kind, logical_path, symbol_signature)` — independent of the `source_item` row.
On rebuild, a vanished identity whose `content_hash` / signature reappears at a new path is a
**rename**: the new artifact carries a `prior_identity_id` link to the old one, edges reattach to the
new artifact, and the old one is invalidated (per §1) rather than orphaned. History survives the
rename. (Detection is deterministic — content hash / signature equality — never an LLM guess.)

### 3. ACL changes propagate at build time

When a source's `acl_teams` changes (even content-unchanged ⇒ cache hit), propagate it to that
source's artifacts in the new build; edge ACL stays the read-time endpoint intersection
(`acl-source-visibility.md`), so a now-restricted endpoint hides its edges automatically. Closes the
`write.py` `TODO(acl-propagation)`.

### 4. Phase-2 gates become enforcing only after §1

`no-ghost-edges` and per-`edge_type` relation precision flip from skipped to enforcing **once
membership is interval-based** — not before, or they block every incremental build on phantom
dangling edges.

## Consequences

- One reversible migration: `kb_build_run.build_seq`, `knowledge_artifact/edge.valid_from_seq` +
  `invalidated_at_seq`, `knowledge_artifact.prior_identity_id`, and the read-path indexes. Backfill:
  existing rows `valid_from_seq = 0`, `invalidated_at_seq = NULL`.
- mcp-server retrieval/graph/provenance queries change from `= kb_version` to interval membership —
  a cross-service contract touch (`docs/contracts/`), so the contracts move first.
- Invariants 1/4/5 all hold: Postgres stays truth; no re-LLM on unchanged content; past versions are
  immutable and reconstructable; MCP serves a complete active set.
- Old versions are prunable by deleting rows with `invalidated_at_seq < oldest_retained_seq` —
  retention becomes a simple sweep, not a special case.

## Why this needs an ADR (not just PR-27)

It changes the **definition of a KB version** and the **artifact identity contract** that retrieval,
provenance, and the publish gates all depend on — a structural change CLAUDE.md requires an ADR for,
and one that spans both services. PR-27 should be **re-scoped on top of this**: §3 (ACL propagation)
is shippable immediately and low-risk; §1/§2/§4 depend on ratifying this model.

## Open question for the ratifier

Confirm the intended production cadence: **incremental nightly** (assumed here, per ADR-0004) makes
§1 mandatory. If production instead does a **full rebuild every night** (every source reprocessed,
every artifact re-created under the new label), the served-set bug cannot occur and §1 reduces to a
deletion/rename sweep only — but that contradicts ADR-0004's cost rationale and the generation-cache
design. This ADR assumes incremental.
