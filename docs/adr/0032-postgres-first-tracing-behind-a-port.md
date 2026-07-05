# ADR-0032 — Traces live in Postgres behind a TraceSink port; no hosted tracing SaaS

## Status

Accepted (2026-07-05, owner directive). **Amends ADR-0030 §4**, which committed to LangSmith
tracing from day one. That commitment is withdrawn before ever activating: LangSmith's SDK is open
source but the tracing service is LangChain's hosted SaaS — the API key is a billing credential,
and the owner's constraints are explicit: the team is Azure-based, and "I do not want a system
where I have to pay just for traces."

## Context

Both owned LangGraph graphs (the `get_task_context` backend and the review-panel draft engine) are
trace-*ready* but nothing ever activated. The platform already has call-level observability (the
retrieval ledger, complete by construction; structured logs; the ADR-0014 dashboard). What deep
traces add is per-step visibility inside a graph run: which node was slow, what the retry did,
how long each reviewer lens took. That does not require a vendor — it requires span rows and a
query surface, both of which this platform already knows how to own.

## Decision

1. **A `TraceSink` port per owning service** — a small Protocol (start/end span or emit-span
   semantics), defined independently in `services/mcp-server` and `services/review-panel`
   (duplicated, never shared, per ADR-0008). Graph code depends only on the port.
2. **`PostgresTraceSink` is the default adapter.** mcp-server spans land in a `trace_span` table
   in the Knowledge Registry (kb-builder owns the migration, reversible per house rules);
   review-panel spans land in its own `review_panel` schema (extending its documented bootstrap
   exemption). Spans carry: trace/span/parent ids, name, start/end, status, and a metadata JSONB
   of durations/token counts/result sizes — **never secrets, never raw prompt or document bodies**
   (same aggregate-only posture as the dashboard).
3. **Tracing is fail-soft, always.** A dead or slow sink logs a structured warning and drops the
   span — it never fails, delays materially, or budget-charges the call it observes
   (observability ranks below answering; the owner's standing directive).
4. **The sink is env-selected** (`TRACE_SINK=postgres|none`; default `postgres` when a database is
   configured, else `none`). Adding Langfuse — the designated future option, genuinely OSS and
   self-hostable, Azure-deployable — is one new adapter class plus config, no graph-code change.
5. LangChain's native `LANGSMITH_*` env instrumentation remains inert in the dependencies; it is
   no longer part of the platform's observability story and is never configured.

## Consequences

- The ADR-0014 dashboard can later grow a "slowest spans / slowest node" tile from `trace_span`
  with plain SQL — same projection posture, no new infra.
- No vendor bill, no data egress; traces sit next to the ledger they correlate with (joinable on
  time/subject/tool).
- One more registry table to migrate (reversible); span volume is bounded by tool-call volume and
  can be pruned by age without losing truth (spans are derived observability, not evidence).
- Langfuse adoption later is additive: adapter + env, plus its own ADR note if it introduces a
  running service.

## Alternatives rejected

- **LangSmith SaaS** — paid per-trace vendor, non-Azure, rejected by the owner explicitly.
- **Self-hosted LangSmith** — enterprise/paid licensing; not open source.
- **An OpenTelemetry collector stack** — real infra (collector + backend) adjacent to the V1
  exclusion list, for no capability Postgres spans don't already give at this scale.
- **Logs-only tracing** — not queryable enough to answer "which node was slow this week" without
  log-scraping; spans-as-rows is exactly the ledger pattern that already works here.
