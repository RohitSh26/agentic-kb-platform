# 0004. Nightly incremental batch build (not streaming)

- Status: Accepted
- Date: 2026-06-10
- Deciders: Platform team

## Context
The KB does not need real-time freshness for V1. Streaming/event pipelines add cost and operational
complexity. Repeated full rebuilds waste LLM/embedding spend.

## Decision
Build the KB nightly via a scheduled CI pipeline, incrementally. Skip unchanged content by content
hash; gate every LLM/embedding call behind generation/embedding caches. Activate a new kb_version
only after validation.

## Consequences
+ Deterministic, cheap, operationally simple; cost scales with change, not corpus size.
+ MCP always serves the last successful active version.
- Freshness is bounded by the nightly cadence (acceptable for V1).

## Implementation status (2026-06-15)
The build plane that this ADR depends on is fully implemented and exercised in PR/push CI (the
`kb-builder build` CLI, incremental skip-by-content-hash, generation/embedding cache gating, the
publish gates, and validation-before-activation). **The scheduled trigger itself is NOT yet wired**:
`.github/workflows/ci.yml` runs on `push`/`pull_request` only — there is no `schedule:`/cron job that
runs the nightly build against a real source set. This is a known, deliberate gap, not an oversight:
a production nightly needs a configured Azure target (DB + source credentials + Search index) which
does not exist in CI today, and a cron job that did nothing useful would only add noise. Tracked as a
follow-up — wire a `schedule:` workflow that runs the build CLI against the production environment
and enforces the publish gates once that environment is provisioned. Until then, invariant 4
(incremental build) and the publish gates are validated by the eval harness and the build-engine
integration tests, not by a live nightly.

## Add-when trigger
Nightly is not fresh enough → add webhook/event-driven ingestion for changed sources (ADR-0007).
