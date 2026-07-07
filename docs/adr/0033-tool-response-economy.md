# ADR-0033 — Tool-response economy: skeleton evidence text + stable, compact responses

- Status: Accepted
- Date: 2026-07-07
- Deciders: platform owner (directive: "learn from headroomlabs-ai/headroom and apply — improve the
  tools we have"), Claude

## Context

Two documented gaps meet one validated external pattern:

1. Our deterministic code-skeleton compressor (ADR-0026, measured **45%** token reduction on this
   repo's real code) runs only in the CLI/eval loop — **nothing in the production MCP path uses
   it** (measured-results finding, 2026-07-05). Production tools economize structurally (pointers,
   snippets, budgets) but the code content they do carry is raw.
2. Layer-2 design calls for byte-stable shared-prefix prompt caching; nothing enforces response
   stability in our tool outputs today, and `get_task_context` repeats the same path strings across
   its sections (scope, blast radius, similar changes) — paying for the same bytes several times.

Headroom (current `headroomlabs-ai/headroom`, re-evaluated 2026-07-07) independently validates
both moves at production scale: its *deterministic* lane (AST `CodeCompressor`, JSON
`SmartCrusher`) reports 47–92% input reduction with accuracy-neutral benchmark deltas, and its
`CacheAligner` keeps the frozen prefix byte-stable so provider KV caches actually hit. Its trained
ML text compressor remains rejected for us (ADR-0026's reasoning stands: lossy transforms are
disqualifying where exact citation matters).

## Decision

1. **Skeleton evidence text at build time.** For code artifacts, the evidence-ready display/search
   text the registry stores becomes the deterministic skeleton (signatures, types, docstrings;
   bodies elided) — produced at build time (content-hash cached, zero model calls) by a
   kb-builder-owned copy of the ADR-0026 compressor. Tool responses that carry code content
   (`kb_search` snippets, future expansions) therefore get ~45% denser for free. **Hard
   constraint:** citation semantics are untouched — evidence pointers still resolve to
   `source_item` (uri/version/span) and the governed L0–L2 verify path keeps working; skeletons
   are for *thinking*, never *citing* (ADR-0026 rule, unchanged).
2. **Stable, compact tool responses.** `get_task_context` (and `kb_search`) responses adopt a
   response-stability discipline: deterministic field and list ordering, stable identifiers early
   and volatile values late, and **cross-section path dedup** (a path appears once in full; other
   sections reference it) — smaller payloads now, KV-cache-friendly prefixes for hosts later.
   Contract-versioned if the wire shape changes.
3. **Measurement is the acceptance bar**: re-run the T3 two-arm A/B after implementation; success
   = tooled tokens measurably down at equal-or-better coverage. Adopt Headroom's honesty device in
   the eval backlog: report savings with a confidence interval, not a point estimate.

## Consequences

- Compression finally moves from prototype lane to the production path, on the platform's own
  terms (deterministic, reversible-by-pointer, provenance-safe).
- One more build step (cheap, cached); a possible contract minor-version bump on response shape.
- Rejected again, for the record: vendoring Headroom or any lossy ML compressor over evidence.

## Alternatives considered

- **Adopt Headroom as a dependency/proxy** — rejected: its value-adds for us are the two ideas
  above, both cheaper to own than to integrate; the ML lane is disqualified; a proxy in front of
  hosts is out of our control surface. Its `wrap` mode remains a legitimate host-side pilot
  experiment (recorded 2026-07-07), separate from platform code.
- **Compress at the MCP server at request time** — rejected: build-time is cheaper (cached once),
  and the server stays zero-transform on the hot path.
