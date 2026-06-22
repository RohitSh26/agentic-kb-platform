# agentic-mcp-server

Remote MCP Context Broker (runtime plane): the policy, retrieval, dedupe,
evidence, and token-budget layer that serves the active KB version to
orchestrator and subagent manifests. Not a thin search wrapper.

The KB is a **preferred-first, budgeted** tool, not a gate (ADR-0025): agents
ask `kb_search` first and read specific files directly when it falls short, and
the governed `create_pack → open_evidence → verify_answer` path is for when an
answer must be citation-grade. Token cost is controlled by a per-task budget
(in the tool, not the prompt) plus skeleton-first code reads (ADR-0026).

## Boundaries

- Self-contained `uv` project; imports nothing from `services/kb-builder` or
  any root package (enforced by `tests/contract/test_import_boundaries.py`).
- **Never runs migrations** — kb-builder owns the Postgres schema. This
  service queries the registry through names pinned in
  `docs/contracts/postgres-knowledge-registry.md` and asserted by
  `tests/contract/test_registry_dependency.py`.
- No build-plane code: no connectors, docify, graphify, linker, or indexing.
- Budgets, evidence levels, and request policy are enforced server-side in
  the broker, never by prompts (tool contracts:
  `docs/contracts/mcp-tools-contract.md`).

## Layout

```
src/agentic_mcp_server/
  mcp/              server assembly, tool_registry, tool_handlers, tool_schemas/
  auth/             Entra ID JWKS bearer-token verification
  telemetry/        one structured log line per tool call (injection-safe)
  domain/           token_budget and broker policy primitives
  infrastructure/   postgres session + pinned registry lookups
  health.py         readiness = an active kb_version exists (invariant 5)
tests/              unit / integration / contract
```

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest                      # integration health tests skip without TEST_DATABASE_URL
```

Integration health tests need a database **already migrated by kb-builder**:
run `make migrate-test-db` from the repo root first. Serve locally with
`python -m agentic_mcp_server` (env vars in `.env.example`).
