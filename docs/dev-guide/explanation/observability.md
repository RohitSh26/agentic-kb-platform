# Observability

The platform answers "what happened, and what did it cost?" from records it writes about itself.
No tracing SaaS, no sidecar, no instrumentation agent: everything observable lives in Postgres,
next to the knowledge it describes. This page explains the model; the commands live in
[read-the-dashboard](../how-to/read-the-dashboard.md) and
[query-traces-and-the-ledger](../how-to/query-traces-and-the-ledger.md).

## Three record stores

**The retrieval ledger (`retrieval_event`)** is the audit record. Every broker tool call writes
exactly one row — including the calls that were refused and the calls that crashed. The `status`
column tells the story:

| Status | Meaning |
|---|---|
| `approved` | new evidence retrieved and charged against a budget |
| `reused` | a repeated question answered from existing evidence, at no budget cost |
| `denied` | a budget said no — a contractual outcome, not an error |
| `needs_human_approval` | a justified request that would exceed the remaining budget |
| `error` | the call failed; the failure is recorded, and any charge was refunded |

The ledger is complete by construction: a crashed call is ledgered exactly once by a uniform
wrapper, and its budget charge is refunded under the same lock that made it. Rows carry tokens,
statuses, artifact ids, and per-tool detail (budget windows for searches, per-node latency for
task context) — never query text and never artifact bodies.

**Traces (`trace_span`, and `review_panel.trace_span` for draft runs)** time the steps inside the
work (ADR-0032). One row is one completed unit: a root span per call, one child span per node
that actually executed, each with a status and start/end times. Attributes are aggregate-only —
counts, booleans, token totals — and the span constructor rejects content-shaped keys, so a
prompt or a query can never land in a trace. Tracing is fail-soft by contract: a dead or slow
sink can never fail, delay, or budget-charge the call it observes; the span is emitted after the
call's own work is done, and a sink error is logged and dropped.

**The build audit (`kb_build_run`)** is one row per build: status, duration, source and artifact
counters, model and embedding call counts (the cache-efficiency signal — an incremental rebuild
should sit near zero), extractor failures, the gate that blocked activation if one did, and the
ledger-mining counters (misses seen, mined, unresolved).

## Derived versus audit

The distinction that decides what you protect:

- **Derived, rebuildable** — the four dashboard views, the rendered dashboard files, the search
  index, and traces. Projections over the record stores. Drop them, prune them, regenerate them;
  nothing is lost.
- **Audit record** — the retrieval ledger and the build-run history. These have no source to
  rebuild from: once gone, that history is gone. This is the backup boundary — the knowledge
  graph itself can always be rebuilt from your sources, but the record of who retrieved what
  cannot ([back-up-and-restore](../how-to/back-up-and-restore.md)).

## The dashboard

Four read-only views project the record stores into questions an operator actually asks —
retrieval health day by day, token economics, build health, and budget adherence — and the
dashboard renders them as one static page. It is aggregate-only by the same posture as the
ledger: statuses, counts, and totals, never content. A `WARN` tile is a prompt to look, not an
alarm; the page tells you which view to query next. Column definitions are contract-pinned, so a
gate and its dashboard tile can never disagree
([observability-dashboard](../../contracts/observability-dashboard.md)).

## The loop closes

Observability here is not just a rear-view mirror — the platform consumes its own records:

- Searches that came back empty or thin surface in the dashboard as the **KB-gap proxy**
  (`kb_search_zero_thin_rate`): the clearest signal of what agents wanted that your knowledge
  base did not have.
- The next build **mines those misses** into alias entries, and reports mined-vs-unresolved on
  the same dashboard, so you can watch gaps get closed build over build.
- Budget adherence compares actual spend against the configured allowances, so budget tuning is
  driven by the ledger, not by feel ([tune-budgets](../how-to/tune-budgets.md)).

## Related

- [Read the dashboard](../how-to/read-the-dashboard.md) — generate it and interpret each tile.
- [Query traces and the ledger](../how-to/query-traces-and-the-ledger.md) — the SQL.
- [Governance and budgets](governance-and-budgets.md) — why these records exist at all.
