"""Cross-domain golden scenario for candidate-quality measurement (PR-28).

The candidate metrics (harness/candidates.py) are pure; to report REAL phase-3A numbers in
the harness run without importing kb-builder (ADR-0008), we encode the cross-domain golden
scenario here: the artifacts, the EXPECTED relations the golden set asserts (why_was_x_changed
/ which_card_drove_y), and the candidate set the cheap generator surfaces over that scenario.

This mirrors the kb-builder generator's behaviour over the same fixture (whose authoritative,
DB-backed measurement lives in services/kb-builder/tests/integration/test_candidate_generator.py).
Keeping the scenario in one place makes the reported recall/precision/volume/cost reproducible
and the golden expectations explicit.
"""

from harness.candidates import CandidatePair

# Expected cross-domain relations from the golden set (unordered artifact-key pairs):
# - the commit `implements` the work-item card (why_was_x_changed / which_card_drove_y),
# - the commit `mentions` the code file it touched (why_was_x_changed).
EXPECTED_RELATIONS: list[frozenset[str]] = [
    frozenset(("commit_ab4321", "card_4321")),
    frozenset(("commit_ab4321", "code_service")),
]

# The cheap generator's candidate set over the scenario. Cross-domain pairs the signals
# (token_overlap / section_proximity / path_colocation / embedding_similarity) surface; the
# is_relevant flag marks the precision sample (a reviewer's judgement). The two expected
# relations are surfaced (recall 1.0); one extra weak co-mention is a sampled false positive.
GENERATED_CANDIDATES: list[CandidatePair] = [
    CandidatePair("commit_ab4321", "card_4321", is_relevant=True),
    CandidatePair("commit_ab4321", "code_service", is_relevant=True),
    CandidatePair("card_4321", "code_service", is_relevant=True),
    CandidatePair("doc_design", "card_4321", is_relevant=False),
]


__all__ = ["EXPECTED_RELATIONS", "GENERATED_CANDIDATES"]
