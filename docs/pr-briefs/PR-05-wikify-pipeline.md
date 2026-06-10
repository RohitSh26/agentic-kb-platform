# PR-05 — Wikify pipeline

## Scope
Chunking, summary/concept/source-backed-fact generation, generation cache integration, artifact writes.

## Context
docs/architecture §5.1, §6, §7. ADR-0005 (evidence later consumes these).

## Files to create
- `apps/kb-builder/src/wikify/chunker.py`, `wikify/generate.py`, `wikify/write.py`.
- ModelClient interface usage for summaries/concepts (faked in tests).

## Contracts
Concept/summary/source_backed_fact shapes in artifact_schemas; output_schema_version wired into the
generation cache key.

## Acceptance criteria
- Wikify runs only on generation_cache miss; a cache hit returns the cached artifact with zero model calls.
- Generated summaries are stored with authority/freshness scores and are marked as interpreted knowledge.

## Required tests
- Cache-miss generates + writes; cache-hit skips model call; chunker determinism.

## Do NOT
- Treat summaries as final truth in any ranking. No direct Azure OpenAI SDK calls (use ModelClient).

## Kickoff prompt
"Implement PR-05. Chunk → generate (cache-gated) → write artifacts. Fake ModelClient in tests; prove
cache-hit makes zero model calls."
