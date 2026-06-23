# Operations & integration guide

How to build, run, and integrate the platform. It has two services:

- `services/kb-builder` — the build plane. Owns the Postgres schema and migrations; ingests sources and
  publishes knowledge-base versions.
- `services/mcp-server` — the runtime plane. Serves the active knowledge base to agents through the MCP
  Context Broker. Never runs migrations.

## Requirements

- Python 3.12, managed with `uv`.
- PostgreSQL (the system of record).
- A model provider for extraction and embeddings (Azure OpenAI or an OpenAI-compatible endpoint; a
  local embedder is available for offline builds).
- Optionally, a managed search service for the derived index (the platform also ships a file-backed
  local index for self-contained runs).

## Configure

Both services read configuration from the environment.

| Variable | Service | Purpose |
|---|---|---|
| `DATABASE_URL` | both | Async Postgres URL (`postgresql+asyncpg://...`). The build plane applies migrations; the runtime plane only reads. |
| `LLM_*` | build | Extraction model provider, model, and credentials. |
| `EMBEDDINGS_PROVIDER` | build | Optional; enables the semantic linking pass. |
| `MCP_*` | runtime | Context Broker host, auth, and budget settings. |

Secrets are referenced by environment variable; never commit them.

## Build a knowledge base

From `services/kb-builder`, with a migrated `DATABASE_URL`:

```bash
uv run alembic upgrade head
uv run python -m agentic_kb_builder.build --workspace . --sources ./sources.example.yaml
```

The build is incremental: unchanged sources make no model calls. A new version is activated only after
validation passes; otherwise the previous version keeps serving.

## Serve the knowledge base

From `services/mcp-server`, against an already-built database:

```bash
uv run python -m agentic_mcp_server
```

The server exposes a health probe and the Context Broker tools. It serves only the single active
knowledge-base version. Authentication is required for every tool call; the health probe is the only
unauthenticated endpoint and discloses only the service name and active version.

## Integrate an agent

Point an MCP-capable coding client at the Context Broker. Agents are knowledge-first: they query the
knowledge base within a budget and read specific files directly only when the knowledge base falls
short. Every served claim is cited, and an answer is trusted only when it carries a provenance receipt.

## Deploy

Each service is an independent project with its own dependencies and container image. The build plane
runs on a schedule (it applies migrations, ingests changed sources, validates, and activates). The
runtime plane runs continuously and scales independently. Infrastructure templates are under `infra/`.

## Operate

- **Health** — the runtime plane returns a 200 once an active version exists, 503 otherwise.
- **Audit** — every retrieval is written to the ledger with the caller, budget use, and evidence
  returned.
- **Recovery** — model outputs are persisted durably during a build, so an interrupted build resumes
  without repeating paid extraction or embedding work.
- **Index rebuild** — the search index is a projection; it can be rebuilt from Postgres without
  re-running any model.
