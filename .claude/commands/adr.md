---
description: Draft an Architecture Decision Record using the author-adr skill.
argument-hint: "<the decision being made>"
---

Use the `author-adr` skill to draft an ADR for: $ARGUMENTS

Pick the next sequential number in `docs/adr/`. Fill Context with concrete evidence (a metric, a
failing eval, a cost signal), not a hunch. If this proposes adding a V1-excluded resource, include
the "add when" trigger and how to add it safely behind the existing MCP interface. Leave Status as
Proposed for human acceptance.
