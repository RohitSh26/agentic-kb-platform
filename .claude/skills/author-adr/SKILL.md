---
name: author-adr
description: >
  Workflow for recording an Architecture Decision Record. Use when proposing any deviation from V1
  (e.g. adding Redis, Blob, a graph DB, streaming) or making a notable design choice. Triggers:
  "write an ADR", "we need to add <excluded resource>", "decision record".
---

# Author an ADR

ADRs live in `docs/adr/NNNN-short-title.md`, numbered sequentially. Use this template:

```
# NNNN. <Title>

- Status: Proposed | Accepted | Superseded by NNNN
- Date: YYYY-MM-DD
- Deciders: <names>

## Context
What forces are at play? What problem or pressure triggered this? Link the evidence — a metric, a
failing eval, a cost signal — not a hunch.

## Decision
The choice, stated plainly.

## Consequences
Positive, negative, and what this commits us to. For anything on the V1 exclusion list, state the
"add when" trigger from the blueprint and how to add it safely behind the existing MCP interface.

## Alternatives considered
Briefly, with why they lost.
```

Rule: adding any V1-excluded resource (Functions, Event Grid, Service Bus, Redis, API Management,
Blob, graph DB, SQLite-as-prod, streaming) REQUIRES an accepted ADR. Default is "no, not yet."
