# Knowledge Platform

A cost-conscious, Postgres-first knowledge platform that serves grounded, cited context to coding
agents through a remote **MCP Context Broker**.

The platform turns your code, docs, wikis, and tickets into a governed knowledge base and serves it to
coding agents with access control, token budgets, and verifiable provenance — so agents answer and
build with cited, current context instead of guessing.

## Two services

```
services/kb-builder   Build plane. Ingests sources, builds the Postgres knowledge graph, and
                      publishes a validated knowledge-base version. Owns the schema + migrations.
services/mcp-server   Runtime plane. Serves the active knowledge base to agents through the MCP
                      Context Broker: auth, access control, ranking, budgets, and provenance.
```

## How it works

- **Postgres is the source of truth.** The search index is a derived projection that can be rebuilt at
  any time.
- **Builds are incremental.** Unchanged sources make no model calls; a new version activates only after
  validation passes.
- **Agents are knowledge-first.** They consult the knowledge base within a budget and read files
  directly only when needed; code reads arrive compressed, with exact text on demand.
- **Everything is governed.** Results are access-filtered, every retrieval is audited, retrieved
  content is untrusted, and every served claim is cited.

## Documentation

- [Architecture](docs/architecture.md) — the two planes, the end-to-end flow, and the invariants.
- [Operations & integration guide](docs/dev-guide.md) — build, run, integrate, deploy, operate.
- [Design decisions](docs/decisions.md) — the rationale behind the key choices.
- [Sequence diagrams](docs/sequence-diagrams.md) — build, retrieval, recovery, activation, verification.

## Quick start

```bash
# build a knowledge base
cd services/kb-builder
uv run alembic upgrade head
uv run python -m agentic_kb_builder.build --workspace . --sources ./sources.example.yaml

# serve it
cd ../mcp-server
uv run python -m agentic_mcp_server
```

See the [operations guide](docs/dev-guide.md) for configuration and deployment.
