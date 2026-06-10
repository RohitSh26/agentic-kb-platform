# 0008. Two self-contained deployable services

- Status: Accepted
- Date: 2026-06-10
- Deciders: Platform team

## Context
The original layout was a uv workspace: apps/kb-builder and apps/mcp-server depending on shared
root packages (common, contracts, db). Shared runtime packages couple deploys: a registry model
change forced both planes to rebuild, the contract boundary was a Python import instead of a
reviewed document, and either service could silently reach the other's internals.

## Decision
Restructure into two fully self-contained uv projects under services/:
- services/kb-builder (package agentic_kb_builder) — build plane. Owns the Postgres schema and
  the only Alembic migrations in the repo.
- services/mcp-server (package agentic_mcp_server) — runtime plane. Never runs migrations and
  never contains build-plane code (connectors, wikify, graphify, linker, indexing).

No root-level Python packages and no root uv workspace. Cross-service agreements live only as
markdown in docs/contracts/ (registry tables, search index, evidence pack, MCP tools, agent
outputs). Small DTOs are duplicated rather than shared. Each service carries import-boundary
contract tests that fail on any cross-service or legacy root-package import, plus tests pinning
the registry names it depends on (mcp-server pins kb_build_run / kb_version / status).

## Consequences
+ Independent deploys and dependency sets: the build plane carries alembic and
  azure-search-documents; the runtime plane carries fastmcp. Neither drags the other's stack.
+ The contract surface is explicit, reviewed markdown enforced by tests on both sides.
- Deliberate duplication (structured logging helper, schema-version constants) must be kept in
  sync manually through docs/contracts.
- Registry schema changes require coordinated contract + test updates in both services.

## Alternatives considered
Keeping the uv workspace with shared packages (rejected: couples deploys and blurs storage
ownership). Publishing shared packages to a private index (rejected for V1: release overhead
with no second consumer).
