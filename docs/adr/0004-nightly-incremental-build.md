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

## Add-when trigger
Nightly is not fresh enough → add webhook/event-driven ingestion for changed sources (ADR-0007).
