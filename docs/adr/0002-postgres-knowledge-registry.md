# 0002. Postgres as the Knowledge Registry and source of truth

- Status: Accepted
- Date: 2026-06-10
- Deciders: Platform team

## Context
We need a durable store for artifacts, lineage, graph edges, caches, build runs, and the retrieval
ledger, with joins and transactional integrity. Local SQLite and Azure Storage Tables do not handle
this well at the scale and shape we need.

## Decision
Azure PostgreSQL Flexible Server is the single source of truth. It owns canonical metadata and normal
text artifacts. Azure AI Search is a derived, rebuildable projection only.

## Consequences
+ Joins, lineage, edge queries, and cache/ledger tables in one consistent store.
+ Search can be rebuilt from Postgres + source pointers at any time.
- Large/binary bodies are out of scope for Postgres (defer to Blob later — ADR-0007).

## Alternatives considered
SQLite (no concurrency/ops story), Storage Tables (poor joins/lineage), Search-as-truth (not durable,
not authoritative).
