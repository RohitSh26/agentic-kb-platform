# 0006. Implementation stack (Python / uv / fastmcp / Alembic)

- Status: Accepted
- Date: 2026-06-10
- Deciders: Platform team

## Context
The blueprint is implementation-agnostic but strongly implies Python (snake_case modules, AST
extraction, SQLite origins). We need concrete, modern, hermetic-testable choices to build against.

## Decision
Python 3.12 with uv; fastmcp (async) for the MCP server; SQLAlchemy 2.x async + asyncpg + Alembic for
the registry; azure-search-documents behind a SearchClient interface; Azure OpenAI behind a
ModelClient interface; ruff + pyright + pytest/pytest-asyncio; GitHub Actions for CI and the nightly
build.

## Consequences
+ Hermetic tests (interfaces let us fake Search/model); fast, reproducible tooling.
+ Clear apps/ vs packages/ boundary; contracts and db are shared.
- Ties V1 to the Python ecosystem; swapping the MCP framework later is a non-trivial change.

## Alternatives considered
Node/TypeScript MCP server (viable; team is Python-leaning), raw psycopg without ORM (more migration
toil).
