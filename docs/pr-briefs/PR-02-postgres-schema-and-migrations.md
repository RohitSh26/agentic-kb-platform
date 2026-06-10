# PR-02 — Postgres schema and migrations

## Scope
SQLAlchemy models + Alembic migrations for the canonical registry tables.

## Context
docs/architecture §6 (schema sketch). .claude/rules/postgres.md. ADR-0002.

## Files to create
- `packages/db/models/` for source_item, knowledge_artifact, knowledge_edge, generation_cache,
  embedding_cache, kb_build_run, retrieval_event.
- `packages/db/alembic/` env + first revision creating all tables with indexes.
- `packages/db/session.py` async engine/session factory reading DATABASE_URL.

## Contracts
Column names and types must match docs/architecture §6 exactly. UUID PKs, timestamptz, explicit FKs.

## Acceptance criteria
- `alembic upgrade head` creates all tables; `downgrade base` removes them cleanly.
- Indexes exist on content_hash, (source_uri, source_version), edge_type, kb_version, and cache keys.
- Round trip `upgrade head → downgrade -1 → upgrade head` passes on a dev DB.

## Required tests
- Model round-trip insert/select for each table against a disposable Postgres (testcontainers or a
  CI service) — skipped gracefully if no DB is configured.

## Do NOT
- Store full raw documents. No application logic beyond models/migrations.

## Kickoff prompt
"Implement PR-02 using the write-migration skill. Reversible migration, indexes per the rule file,
verify the round trip."
