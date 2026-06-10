---
name: write-migration
description: >
  Workflow for authoring a reversible Alembic migration for the Postgres Knowledge Registry. Use
  whenever a change adds or alters a table, column, index, constraint, or enum. Triggers: "add a
  table", "migration", "schema change".
---

# Write a reversible migration

1. Make the model change in
   `services/kb-builder/src/agentic_kb_builder/infrastructure/postgres/models/` first.
   (kb-builder owns the schema; mcp-server never runs migrations.)
2. From `services/kb-builder`: `uv run alembic revision --autogenerate -m "<short scope>"`.
3. **Hand-edit** the generated file. Autogenerate misses: index changes, server defaults, enum
   alterations, and column type narrowing. Make `upgrade()` minimal and correct.
4. Write a real `downgrade()` that fully reverses `upgrade()`. Never leave `pass`.
5. Match the canonical schema: UUID PKs, `timestamptz`, explicit FKs. Index the columns the build and
   ledger query: `content_hash`, (`source_uri`,`source_version`), `edge_type`, `kb_version`, and each
   cache key.
6. Verify the round trip on a disposable dev DB:
   ```
   uv run alembic upgrade head
   uv run alembic downgrade -1
   uv run alembic upgrade head
   ```
7. Note any data-migration risk and the rollback procedure in the PR description.

Never edit a migration that has already been applied to a shared environment — add a new one.
