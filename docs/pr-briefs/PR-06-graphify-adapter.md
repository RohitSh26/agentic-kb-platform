# PR-06 — Graphify adapter

## Scope
Parse/import Graphify output into canonical code artifacts (code_file, code_symbol, endpoint, test)
and edges (imports, calls, tests, exposed_as).

## Context
docs/architecture §5.2, §4 (artifact + edge types), §7 (code graph cache key).

## Files to create
- `apps/kb-builder/src/graphify_adapter/parse.py`, `graphify_adapter/to_artifacts.py`,
  `graphify_adapter/to_edges.py`.

## Contracts
code_symbol/endpoint/test artifact shapes; edge writes carry confidence, source='graphify', kb_version.

## Acceptance criteria
- Graphify runs only for changed code files (keyed by repo+commit_sha+file_path+file_content_hash+...).
- Produces symbols with exact path + span so L2 evidence can return precise snippets.

## Required tests
- Changed-file-only execution; artifact/edge creation from a fixture graph; cache-key correctness.

## Do NOT
- Add a graph database. Edges go to knowledge_edge in Postgres.

## Kickoff prompt
"Implement PR-06. Import Graphify output into canonical code artifacts/edges in Postgres. Changed
files only; precise symbol spans."
