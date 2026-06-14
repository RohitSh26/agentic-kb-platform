# PR-27 — Identity-over-time: deletion/rename/permission invalidation + enforcing relation gates

## Why

Cross-domain edges create a new failure mode: **ghost edges** — links to artifacts that were
deleted, renamed, or had their permissions changed. The judge flagged rename/identity-over-time
invalidation as a newly surfaced gap. This PR makes a rebuild reconcile identity so no edge ever
references a stale artifact, and promotes the phase-2 publish gates to **enforcing**.

## Scope

- **Identity-over-time:** a stable artifact identity across rebuilds (path + symbol signature; carry
  a prior-identity link on rename so history/edges survive a rename rather than orphaning). On
  rebuild:
  - **deletion** — source/file gone ⇒ its artifacts and every edge touching them are invalidated in
    the new `kb_version` (not carried forward); generation/embedding cache entries for them retired.
  - **rename** — detect (same content_hash / signature, new path) ⇒ reattach edges to the renamed
    artifact instead of dropping them; record the rename.
  - **permission change** — `acl_teams` change on a source propagates to its artifacts and
    recomputes edge intersections; an edge that loses a now-restricted endpoint becomes invisible.
- **Enforcing gates (`publish-gates.md`):** per-`edge_type` relation precision ≥ 0.9 (for relations
  in production) and **no ghost edges** become hard gates — a build with a dangling/ghost edge or a
  sub-threshold relation cannot activate.
- Tests: delete a file → its edges gone in the new version, last active still serves; rename a file →
  edges reattach, no ghosts; tighten a source ACL → dependent edges hidden; ghost-edge / low-precision
  build is blocked by the gate.

## Do NOT

- Do not mutate a published `kb_version` in place — invalidation happens by building a new version;
  versions stay immutable (invariant 5).
- No LLM. No new edge types.

## Acceptance criteria

- [ ] Deleted source ⇒ no surviving edges to its artifacts in the new version; prior version intact.
- [ ] Rename detected ⇒ edges reattach to the renamed artifact (no ghost, no orphan); rename recorded.
- [ ] ACL tightening propagates and recomputes edge visibility.
- [ ] No-ghost-edges + relation-precision gates block a bad build (test).
- [ ] `make verify` + `make eval-run` green.
