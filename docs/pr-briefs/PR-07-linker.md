# PR-07 — Linker

## Scope
Connect Wikify concepts/docs/cards to Graphify code artifacts and tests using deterministic +
semantic strategies, with confidence-scored edges.

## Context
docs/architecture §5.3 (example relationship set), §4 (edge types), §14 (noisy-edge risk).

## Files to create
- `apps/kb-builder/src/linker/deterministic.py` (path/ref matching), `linker/semantic.py` (embedding
  similarity), `linker/write_edges.py`.

## Contracts
documents/implements/requests/exposed_as/tests/mentions edges with confidence + source='linker'.

## Acceptance criteria
- Produces the canonical example chain for a fixture (concept → wiki → ADO → code symbol → endpoint →
  test).
- Every edge stores confidence and source; low-confidence edges are flagged for eval.

## Required tests
- Deterministic match wins over semantic; confidence thresholds; no duplicate edges on rerun.

## Do NOT
- Over-link. Bias toward precision; record uncertain links as low confidence, not as facts.

## Kickoff prompt
"Implement PR-07. Deterministic-first linking with semantic fallback, confidence-scored edges, no
duplicates on rerun."
