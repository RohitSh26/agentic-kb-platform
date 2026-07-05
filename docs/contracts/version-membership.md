# Contract: kb_version membership + identity-over-time

> Cross-service contract realising ADR-0013. kb-builder (producer) stamps the validity interval
> and runs the invalidation pass; mcp-server (consumer) serves by **interval membership** instead
> of `kb_version` label-equality. Both services implement the **identical** membership predicate
> below. This document is the only thing the two services share — duplicate the small predicate,
> never the code (ADR-0008).

## Why this exists

Every `knowledge_artifact` / `knowledge_edge` is stamped with one `kb_version` **at creation and
never re-stamped**. On a generation-cache hit the build returns the cached rows unchanged, so they
keep the label of the build that first created them. mcp-server retrieval scoped strictly
`WHERE kb_version = :active`. An incremental build `N` re-creates only the *changed* rows (label
`N`); everything unchanged keeps labels `< N`. Activating `N` then served only that day's delta —
the served KB silently shrank on every incremental build, defeating ADR-0004 and invariant 5.

The fix: a KB version is an **interval membership**, not a creation label. `kb_version` stays on the
row for labelling/logging only.

## Build sequence

`kb_build_run.build_seq` is a **monotonic** `BIGINT`, assigned once at run start (from the
`kb_build_seq` Postgres sequence). It is `UNIQUE`. The active build is the `kb_build_run` row with
`status = 'active'`; its `build_seq` is the **served sequence `S`**. The `kb_version` label maps to
its `build_seq` via the `kb_build_run` row.

## Validity interval (stamped once, immutable)

Each artifact/edge row carries:

- `valid_from_seq BIGINT NOT NULL` — the `build_seq` that **introduced** the row (set at creation;
  default `0` for rows that predate this model). A cache-hit-carried row keeps its **original**
  `valid_from_seq` — it has been a member since introduction.
- `invalidated_at_seq BIGINT NULL` — `NULL` while live; set to build `N` when the row **leaves** the
  KB in build `N` (deletion or rename-away). Setting it **never** mutates any past version: the row
  stays a member of every version `< N`, so prior active versions remain byte-reconstructable
  (invariant 5). A published version's rows are **never** UPDATEd in place beyond this one-time
  `NULL → N` transition, and live rows a prior version serves are **never** physically DELETEd.

## The membership predicate (identical in both services)

A row is a **member of the version whose `build_seq = S`** iff:

```
valid_from_seq <= S AND (invalidated_at_seq IS NULL OR invalidated_at_seq > S)
```

mcp-server resolves the active build's `build_seq` **once** (from `kb_build_run WHERE
status='active'`) and filters **every** artifact / edge / provenance / graph-neighbour / search
query by this predicate **instead of** `kb_version = :active`. kb-builder's publish gates evaluate
"the served set" of the build under gate with the same predicate against that build's `build_seq`.

## Stable identity + rename link

Artifact identity is `(source_kind, logical_path, symbol_signature)`, independent of the
`source_item` row. On rebuild, a vanished identity whose `content_hash` / signature reappears at a
**new** path is a **rename** (deterministic — content-hash / signature equality, never an LLM
guess):

- the new artifact carries `prior_identity_id` → the old artifact's `artifact_id`;
- edges are reattached to the new artifact (or recomputed by the linker so they point at it);
- the old artifact is invalidated (`invalidated_at_seq = this build_seq`) rather than orphaned.

History survives the rename: `prior_identity_id` chains the lineage and the old artifact stays a
member of every prior version.

## Invalidation pass (end of build, BEFORE activation)

Version-scoped — never a physical DELETE of live rows that prior versions serve. The pass is
**guarded**: it runs only after the build re-verifies its own `kb_build_run` row still exists. A
missing row means the registry was reset or swapped underneath the running build (e.g. a
drop/recreate of the database while an older build's connection pool silently reconnects to the
recreated name — the 2026-07-05 zombie-build incident); the build must abort loudly
(`build_run_row_missing`) instead of reconciling a world it never observed.

1. **Deletion sweep** — any `source_item` not seen in this build's connector listing **and last
   recorded strictly before this build started** (`COALESCE(last_seen_at, created_at) <
   kb_build_run.started_at`) ⇒ mark `is_deleted = true` AND set `invalidated_at_seq =
   this_build_seq` on its still-live artifacts and on every still-live edge touching them; retire
   their generation/embedding cache rows. The time fence is the **concurrent-writer guard**: a live
   source this build never saw but that was created/touched at-or-after `started_at` was observed
   by some other writer, so this build's listing cannot prove it vanished — it is skipped, counted
   as `concurrent_sources_skipped`, and logged as a WARNING (`deletion_sweep_concurrent_skip`),
   because it is direct evidence that two builds are interleaving on one registry.
2. **Rename detection** — as above; its "vanished" candidates come from the same guarded set, so a
   concurrently-written source is never mistaken for a renamed-away one either.
3. **ACL propagation** — when a source's `acl_teams` changed (even content-unchanged ⇒ cache hit),
   update its live artifacts' `acl_teams` this build. Edge ACL stays the read-time endpoint
   intersection (`acl-source-visibility.md`), so a now-restricted endpoint hides its edges
   automatically.

## Idempotency

A rebuild on unchanged inputs produces **no** invalidations and **no** churn: a source still seen is
not swept, a content-unchanged source's `acl_teams` write is a no-op, and the membership of every
live row is unchanged.
