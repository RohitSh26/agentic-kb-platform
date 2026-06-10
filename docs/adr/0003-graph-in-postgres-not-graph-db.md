# 0003. Graph model in Postgres; no graph database in V1

- Status: Accepted
- Date: 2026-06-10
- Deciders: Platform team

## Context
The KB is graph-shaped (concepts ↔ docs ↔ code ↔ tests ↔ endpoints). We want graph behavior without
prematurely adopting a graph database and its cost/operational/schema-churn burden.

## Decision
Store nodes and edges in Postgres (knowledge_edge with edge_type, confidence, source, kb_version).
Expose graph behavior exclusively through MCP graph tools (e.g. graph.get_neighbors), so the backend
can be replaced later without changing agents.

## Consequences
+ One-hop/two-hop expansion works now; no new datastore.
+ Future migration to a graph DB is hidden behind the MCP graph interface.
- Deep traversal / path queries / graph analytics are not first-class until we add a graph DB.

## Add-when trigger
Runtime deep traversal, impact analysis, or graph analytics become core product features.
