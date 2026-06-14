# PR-25 — Golden-query evals (evidence-recall) + publish gates wired to the build

## Why

Design A's silent failure mode is **underlinking** — real citations that miss the key symbol/ADR/card
— and happy-path tests don't catch it. This PR makes **evidence-recall** a first-class metric over a
golden-query set and wires the concrete **publish gates** into the build so a `kb_version` activates
only when it passes (`docs/contracts/golden-query-evals.md`, `publish-gates.md`). Completes phase 1.

## Scope

- **evals harness:** the golden-query case shape (`case_id`, `query`, `intent`, `requester_teams`,
  `expected_evidence_ids`, optional `expected_edge_types` / `must_not_leak_ids`, `min_evidence_recall`)
  and the **evidence_recall** metric (generalises `missing_context_rate`). Per-`edge_type`
  precision/recall reporting. `acl_leak_count` from `must_not_leak_ids` (must be 0).
- **Seed golden set:** a handful of code-structure golden queries over the PR-22 fixture workspace
  (where-is-X-defined, what-calls-Y, imports-of-Z) with their expected evidence ids — doubles as the
  graphify acceptance test.
- **Publish gates in the build (`publish-gates.md`):** index consistency (existing) + extractor
  error rate + symbol-count delta (overridable via `allow_large_delta`) + evidence-recall ≥ 0.95 +
  `acl_leak_count == 0` + no dangling citations + edge evidence integrity. A failed, non-overridden
  gate leaves the new `kb_version` inactive, records which gate + the measured value in
  `kb_build_run`, and MCP keeps serving the last active version. Manual `activate <kb_version>` for
  re-activation.
- Tests: a build that passes all gates activates; a build with an injected dangling citation / a
  forced low evidence-recall / a forced ACL leak stays inactive with the failing gate recorded; the
  `allow_large_delta` override is honoured and logged.

## Do NOT

- Do not make relation-precision or no-ghost-edges enforcing yet (phase 2) — they are skipped, not
  failed, in phase 1.
- Do not couple the gates to Azure; evals run against Postgres + the local indexer.

## Acceptance criteria

- [ ] `evidence_recall` + per-edge-type precision/recall + `acl_leak_count` reported by the harness.
- [ ] Seed golden queries pass against a correctly built fixture; removing a key edge drops recall
      below the gate and blocks activation (test).
- [ ] A failing, non-overridden gate leaves the version inactive and records the reason in
      `kb_build_run`; last active version keeps serving.
- [ ] `allow_large_delta` override works and is logged.
- [ ] `make verify` + `make eval-run` green.
