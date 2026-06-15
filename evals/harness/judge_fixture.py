"""Cross-domain judge scenario for phase-3B quality measurement (PR-29).

Mirrors candidate_fixture.py: it encodes the same cross-domain golden scenario plus the
DETERMINISTIC linker baseline and the judge's INFERRED edge set over that scenario, so the
harness reports REAL inferred-edge precision + cross-domain evidence-recall LIFT without
importing kb-builder (ADR-0008). The authoritative DB-backed behaviour lives in
services/kb-builder/tests/integration/test_judge.py.

Scenario (same artifacts as the candidate fixture):
- the deterministic linker already finds commit->card (implements) and commit->code (mentions);
- the doc->code relationship has NO deterministic key (prose only), so only the LLM judge can
  surface it as an INFERRED `documents` edge — that is the cross-domain recall LIFT.
"""

from harness.judge import JudgedEdge

# The full cross-domain golden expectation set (unordered pairs), incl. the prose-only
# doc->code relation the deterministic linker cannot reach.
EXPECTED_RELATIONS: list[frozenset[str]] = [
    frozenset(("commit_ab4321", "card_4321")),
    frozenset(("commit_ab4321", "code_service")),
    frozenset(("doc_design", "code_service")),
]

# What the DETERMINISTIC linker already found (work-item / changed-file keys).
DETERMINISTIC_PAIRS: list[frozenset[str]] = [
    frozenset(("commit_ab4321", "card_4321")),
    frozenset(("commit_ab4321", "code_service")),
]

# The judge's verdicts over the candidate set. The prose-only doc->code pair is judged
# INFERRED_HIGH (a real `documents` relation), lifting recall; a weak doc->card co-mention
# is REJECTED (never an edge). is_relevant marks the precision sample.
JUDGED_EDGES: list[JudgedEdge] = [
    JudgedEdge("doc_design", "code_service", "INFERRED_HIGH", is_relevant=True),
    JudgedEdge("doc_design", "card_4321", "REJECTED", is_relevant=False),
]


__all__ = ["DETERMINISTIC_PAIRS", "EXPECTED_RELATIONS", "JUDGED_EDGES"]
