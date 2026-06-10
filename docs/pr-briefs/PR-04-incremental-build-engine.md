# PR-04 — Incremental build engine

## Scope
Build-run orchestration: hash comparison, skip-unchanged, build-run records, active kb_version handling.
Wikify/Graphify are called through interfaces that are stubbed in this PR.

## Context
docs/architecture §7 (algorithm + cache keys). ADR-0004. .claude/rules/connectors.md.

## Files to create
- `apps/kb-builder/src/build/runner.py` — implements the 8-step algorithm; writes kb_build_run rows.
- `build/active_version.py` — marks a kb_version active only after a validation hook passes.
- `build/cache.py` — generation_cache + embedding_cache lookups (gate model/embedding calls).

## Contracts
Generation/embedding cache key composition exactly per docs/architecture §7.

## Acceptance criteria
- Unchanged content_hash ⇒ chunk/wikify/graphify/embed/index all skipped (assert via spies).
- Re-running a build is idempotent (no duplicate artifacts/edges/cache rows).
- A failed validation never flips active kb_version; previous version remains served.

## Required tests
- Skip-on-unchanged, idempotency, cache-hit-prevents-call, validation-gates-activation.

## Do NOT
- Implement real Wikify/Graphify/indexer. No streaming/event services.

## Kickoff prompt
"Implement PR-04. Focus on incremental skip logic, idempotency, cache gating, and active-version
safety. Stub Wikify/Graphify/indexer behind interfaces."
