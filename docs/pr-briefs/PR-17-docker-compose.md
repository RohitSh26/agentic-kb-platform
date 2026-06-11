# PR-17 — Docker compose for the whole system (two service containers + Postgres)

## Scope

A root `docker-compose.yml` that spins up the full local system with one command: **Postgres 16**
(the truth store), a **one-shot kb-builder container** (applies Alembic migrations and exits — it
owns the schema), and the **mcp-server container** (starts only after migrations complete). Plus
`.dockerignore` files for both services and a contract test pinning the compose invariants. The
per-service Dockerfiles already exist (PR-01) and are reused unchanged unless a fix is required to
build.

## Context

- kb-builder's image default CMD is `alembic upgrade head`; there is no build CLI yet (nightly
  pipeline follow-up), so the container's job in compose is migrations only — documented honestly.
- mcp-server never runs migrations (ADR-0008); compose encodes this as
  `depends_on: kb-builder: condition: service_completed_successfully`.
- Auth is fail-closed (invariant 6, no auth-off switch); JWKS is fetched lazily, so the stack
  boots with placeholder Entra identifiers — `/health` works, tool calls require a real tenant.
- A fresh registry returns 503 from `/health` (`no_active_kb_version`) — readiness honesty, not a
  failure; no container healthcheck on mcp-server for this reason.

## Changes

- `docker-compose.yml` (repo root) — `postgres:16` with `pg_isready` healthcheck and a named
  volume; host port `${POSTGRES_HOST_PORT:-55432}` (not 5432 — avoids clashing with a local
  Homebrew Postgres); kb-builder one-shot (`restart: "no"`, depends on healthy postgres);
  mcp-server on `${MCP_HOST_PORT:-8000}`, Entra identifiers from
  `${MCP_ENTRA_TENANT_ID:-<placeholder>}` / `${MCP_ENTRA_AUDIENCE:-<placeholder>}` (identifiers,
  never secrets). In-network `DATABASE_URL` uses `postgresql+asyncpg://` and the compose-internal
  `postgres:postgres` local default (same as CI).
- `services/kb-builder/.dockerignore`, `services/mcp-server/.dockerignore` — exclude tests,
  caches, and virtualenvs from build contexts.
- `services/kb-builder/tests/contract/test_compose_contract.py` (kb-builder has pyyaml) —
  skipif-guarded like the portable-exports test: services are exactly
  `{postgres, kb-builder, mcp-server}` (no V1-excluded container can sneak in); postgres image is
  16.x; kb-builder is the only service/Dockerfile that mentions alembic; mcp-server waits on
  `service_completed_successfully`; every `DATABASE_URL` uses the asyncpg driver; no literal
  credential beyond the documented compose-internal local default.
- `docs/dev-guide/03-local-testing.md` — a "Docker" section: one-command spin-up, what each
  container does, the placeholder-identifier and 503-on-fresh-registry honesty notes.

## Acceptance criteria

- `docker compose up` brings up postgres → migrations → mcp-server in order; mcp-server answers
  on `/health` (503 `no_active_kb_version` on a fresh volume is the expected first answer).
- `docker compose config` validates; both images build.
- Contract test pins the service set, the migrations-ownership split, the asyncpg driver, and the
  dependency order.
- `make verify` stays green; no canon, contract, or runtime code changes.

## Do NOT

- Add any V1-excluded container (Redis, Azurite/Blob, graph DB, queues) — the compose service set
  is pinned by test.
- Wire a fake build entrypoint into kb-builder — migrations-only until the nightly CLI lands.
- Put any secret value in compose or env examples; Entra values are identifiers and the Postgres
  password is the documented compose-internal local default only.
- Change the production deployment story — `infra/` (Azure) remains the deployment path; compose
  is a local convenience.
