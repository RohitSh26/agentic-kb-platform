# PR-03 — Connector skeletons

## Scope
Interfaces and deterministic hashing for GitHub code, GitHub docs, Azure Wiki, and ADO connectors.
No real network calls beyond a thin fetch boundary that can be faked.

## Context
docs/architecture §5, §7. .claude/rules/connectors.md.

## Files to create
- `apps/kb-builder/src/connectors/base.py` — `Connector` protocol: `list_sources()`,
  `fetch(source) -> NormalizedContent`, returns source_uri, source_version, normalized text.
- `connectors/{github_code,github_doc,azure_wiki,ado_card}.py` skeletons.
- `packages/common/hashing/content_hash.py` — deterministic normalize+hash.

## Contracts
NormalizedContent and source identity shapes in `packages/contracts/artifact_schemas/`.

## Acceptance criteria
- Same input ⇒ identical content_hash across runs and machines (assert in tests).
- Each connector returns source_type, source_uri, source_version, content_hash.

## Required tests
- Determinism test for content_hash; a fake fetch backend per connector.

## Do NOT
- Implement Wikify/Graphify here. No real credentials in code or fixtures.

## Kickoff prompt
"Implement PR-03 per the brief. Deterministic hashing + fakeable fetch boundary. Tests prove
determinism."
