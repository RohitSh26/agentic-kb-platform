# PR-09 — MCP server base

## Scope
fastmcp server: auth, health endpoint, tool registration scaffold, telemetry, configuration. No
broker logic yet.

## Context
docs/architecture §8, §12. .claude/rules/mcp-tools.md. ADR-0001, ADR-0006.

## Files to create
- `apps/mcp-server/src/server.py` (fastmcp app, health), `src/auth/` (Entra ID / managed identity
  boundary), `src/telemetry/` (structured logs + metrics), `src/config.py`.

## Contracts
Tool registration reads request/response schemas from packages/contracts/mcp_schemas (tools stubbed).

## Acceptance criteria
- Health endpoint returns active kb_version.
- Auth boundary rejects unauthenticated calls; managed identity used for downstream access.
- Every request emits a structured log line with run/agent/tool/latency.

## Required tests
- Health, auth-required, telemetry emission. Tool stubs return "not implemented".

## Do NOT
- Implement create_pack/request_more here. No API Management. No secrets in code.

## Kickoff prompt
"Implement PR-09: fastmcp base with auth, health (returns active kb_version), telemetry, config. Tools
registered but stubbed. Have security-auditor review the auth boundary."
