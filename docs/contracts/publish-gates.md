# Contract: Publish + rollback gates

> Cross-service contract. kb-builder runs the gates at the end of a build; a `graph_version`
> (`kb_version`) is marked active ONLY if all gates pass. mcp-server keeps serving the previous
> active version until then. Formalises invariant 5 ("active only after validation passes").

## Why

"Active only after validation" needs concrete, automated checks — otherwise a build that silently
drops symbols or leaks ACLs could go live. These gates make the publish decision deterministic and
the rollback automatic (the previous active version is never touched until the new one passes).

## Gates (all must pass to activate)

| gate                      | check                                                                                          | phase |
|---------------------------|------------------------------------------------------------------------------------------------|-------|
| index consistency         | Search projection rebuildable from Postgres; counts match (existing validation)                | 1     |
| extractor error rate      | files that failed AST extraction / total ≤ threshold (default 1%)                              | 1     |
| symbol-count delta         | |symbols(new) − symbols(prev)| / symbols(prev) ≤ threshold (default 25%) unless override flag   | 1     |
| evidence-recall            | golden-query `evidence_recall ≥ 0.95` (`golden-query-evals.md`)                                | 1*    |
| ACL leak                  | golden-query `acl_leak_count == 0`                                                              | 1     |
| no dangling citations      | every citeable fact's evidence pointer resolves within the new version                          | 1     |
| edge evidence integrity    | every `knowledge_edge` has a valid evidence pointer + an allowed `edge_type` (ontology)         | 1     |
| relation precision         | per-`edge_type` `edge_precision ≥ 0.9` for relations in production                              | 2 (enforcing) |
| no ghost edges             | invalidation ran: no edge references a deleted/renamed artifact (`identity-over-time`)          | 2 (enforcing) |

Gates marked "phase" `2 (enforcing)` are **enforcing as of PR-27 / ADR-0013** — once membership is
interval-based (`version-membership.md`), they can be evaluated over the **served set** of the build
under gate without flagging legitimate cross-version edges, so they now block activation.

`*` Phase 1 ships the evidence-recall gate against the seed golden set; it becomes strict as the set
grows. A gate not yet applicable to the current phase is skipped, not failed.

**Phase-1 wiring (PR-25):** the index-consistency, extractor-error-rate, symbol-count-delta,
no-dangling-citations, and edge-evidence-integrity gates are REAL and ENFORCED inside activation
(kb-builder `application/publish_gates.py`, composed with the existing `make_consistency_validator`
into one `ValidationHook`). The evidence-recall + ACL-leak gate is an intentional, documented
**seam**: its authoritative value needs the full Context Broker (retrieval + ACL + budget) over the
golden set, which lives in `evals/` and cannot be imported by kb-builder (service boundary,
ADR-0008). So evidence-recall is enforced by the evals harness (`make eval-run`,
`harness/golden.py` — `evidence_recall`, `acl_leak_count`, per-`edge_type` precision/recall) and is
SKIPPED (logs a registry-derivable proxy, never blocks) inside activation in phase 1. It tightens to
enforcing through the same seam as the golden set grows.

**Phase-2 wiring (PR-27, ADR-0013):** with interval membership in place
(`version-membership.md`), the **no-ghost-edges** gate is now REAL and ENFORCING: every edge that is
a **member** of the build under gate's `build_seq` must have both endpoints also members of that
`build_seq` (no edge to an invalidated/absent artifact). It is scoped by the membership predicate,
not `= kb_version`. The **relation-precision** gate's registry-derivable integrity part (every
edge that is a member carries an allowed `edge_type` from the closed ontology and a resolvable
evidence pointer) is enforcing here over members; its authoritative per-`edge_type` precision over a
labelled golden set stays on the same evals-harness seam as evidence-recall (`make eval-run`).

## Override

A build may set `allow_large_delta=true` (recorded in `kb_build_run`) to bypass the symbol-count
delta gate for an intentional large change (e.g. first build, big refactor). No other gate is
overridable. The override and reason are logged.

## Rollback semantics

- The new version is built into Postgres with its own `kb_version`; the previous active version is
  untouched during the build.
- If any non-skipped, non-overridden gate fails, the new version is left **inactive** and the
  failure (which gate, the numbers) is recorded in `kb_build_run`. MCP keeps serving the last active
  version. No manual rollback step is needed — activation simply never happened.
- A previously active version can be re-activated (manual `activate <kb_version>`) since versions are
  immutable and retained.

## Logging

Each gate's pass/fail + measured value is written to `kb_build_run` (structured). A failed publish is
a first-class, queryable outcome, not a silent skip.
