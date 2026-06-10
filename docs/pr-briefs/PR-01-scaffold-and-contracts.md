# PR-01 — Project scaffold and contracts

## Scope
Stand up the repo skeleton, the uv workspace, lint/type/test config, and the shared contracts package.
No business logic.

## Context
docs/architecture §2–3, §11. ADR-0006 (stack). CLAUDE.md repo map.

## Files to create
- `pyproject.toml` (uv workspace; ruff, pyright, pytest config), per-package `pyproject.toml`.
- `packages/contracts/` with empty-but-typed modules: `mcp_schemas/`, `artifact_schemas/`,
  `agent_output_schemas/`, each exporting versioned pydantic models (start with version constants).
- `packages/common/` with `hashing/`, `logging/`, `token_budgeting/` stubs (typed signatures + tests).
- `apps/mcp-server/src/` and `apps/kb-builder/src/` package skeletons with `__init__` and a health stub.
- `.editorconfig`, `.gitignore`, `Makefile` or `justfile` wrapping `/verify` commands.

## Contracts
Define `output_schema_version`, `prompt_version`, `chunker_version`, `graphify_version` as constants
in contracts so cache keys can reference them.

## Acceptance criteria
- `uv sync` succeeds; `uv run ruff check`, `uv run pyright`, `uv run pytest` all pass on an empty suite.
- Importing `packages.contracts` exposes the three schema namespaces.
- CI workflow runs the same three gates on push.

## Required tests
- A trivial test per package proving imports and the `hashing` helper is deterministic.

## Do NOT
- Add any runtime logic, DB access, or cloud SDKs yet. No V1-excluded resources.

## Kickoff prompt
"Implement PR-01 per docs/pr-briefs/PR-01-scaffold-and-contracts.md. Contracts and tooling only.
Run /verify before reporting done."
