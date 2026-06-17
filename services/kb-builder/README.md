# agentic-kb-builder

Nightly incremental KB build plane: connectors → docify → graphify → linker →
indexing. **Owns the Postgres Knowledge Registry schema** — Alembic migrations
live in `migrations/` and run only from this service.

## Boundaries

- Self-contained `uv` project; imports nothing from `services/mcp-server` or
  any root package (enforced by `tests/contract/test_import_boundaries.py`).
- No MCP runtime code: no fastmcp, no broker, no Evidence Pack serving.
- Postgres is the source of truth; Azure AI Search is a derived, rebuildable
  projection written through the `SearchClient` interface.
- Cross-service contracts are markdown only: `docs/contracts/*.md`.

## Layout

```
src/agentic_kb_builder/
  application/      build_runner, cache_gates, active_version
  connectors/       deterministic source connectors (github, azure wiki, ado)
  docify/           document extraction pipeline (Graphify LLM, cache-gated)
  graphify/         whole-tree code-graph extraction
  linker/           cross-artifact edge linking
  indexing/         search projection, upsert, consistency validation
  domain/           artifact models, content_hasher, schema_versions
  infrastructure/   postgres (models, session), azure_openai, azure_search
migrations/         Alembic revisions (forward + rollback)
tests/              unit / integration / contract
```

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest                      # integration tests skip without TEST_DATABASE_URL
```

Migrations: `uv run alembic upgrade head` (uses `DATABASE_URL`; see
`.env.example`). Every revision has a working `downgrade`.

Local testing never requires Azure: model and search clients are Protocols
with in-repo fakes (`FakeSearchClient`).
