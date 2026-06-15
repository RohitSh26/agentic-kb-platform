"""Cross-domain candidate generator (PR-28, ADR-0010 phase 3A) — pure coverage.

DB-free coverage of the cheap, deterministic, ZERO-LLM generator
(docs/contracts/relationship-candidates.md):
- cross-domain pairs are emitted; same-domain pairs and already-linked pairs are NOT;
- each candidate records its FIRING signals + scores;
- fan-out is BOUNDED per `from` artifact (<= K);
- the generator is DETERMINISTIC for fixed inputs (stable ordering + tie-breaks);
- the embedding signal is None-safe (provider None ⇒ signal simply does not fire).
"""

import uuid
from collections.abc import Sequence

import pytest

from agentic_kb_builder.linker.candidates import (
    CANDIDATE_FAN_OUT_K,
    artifact_domain,
    generate_candidates,
)
from agentic_kb_builder.linker.records import LinkableArtifact
from agentic_kb_builder.linker.semantic import ScoredArtifact, SimilarityProvider

NO_PAIRS: frozenset[frozenset[uuid.UUID]] = frozenset()


def _artifact(
    artifact_type: str,
    *,
    title: str | None = None,
    body_text: str | None = None,
    source_type: str = "github_code",
    external_id: str | None = None,
    path: str | None = None,
) -> LinkableArtifact:
    return LinkableArtifact(
        artifact_id=uuid.uuid4(),
        artifact_type=artifact_type,
        title=title,
        body_text=body_text,
        source_type=source_type,
        external_id=external_id,
        path=path,
    )


def _code_file(path: str) -> LinkableArtifact:
    return _artifact("code_file", title=path, source_type="github_code", path=path)


def _card(external_id: str, *, title: str, body_text: str = "") -> LinkableArtifact:
    return _artifact(
        "summary", title=title, body_text=body_text, source_type="ado_card", external_id=external_id
    )


def _doc(title: str, body_text: str) -> LinkableArtifact:
    return _artifact("summary", title=title, body_text=body_text, source_type="github_doc")


class _StubProvider:
    """Returns a fixed similar-artifact map keyed by source artifact id."""

    def __init__(self, mapping: dict[uuid.UUID, list[ScoredArtifact]]) -> None:
        self._mapping = mapping

    async def similar_code_symbols(
        self, *, artifact_id: uuid.UUID, top_k: int
    ) -> Sequence[ScoredArtifact]:
        return self._mapping.get(artifact_id, [])[:top_k]


def test_domain_classification() -> None:
    assert artifact_domain(_code_file("src/a.py")) == "code"
    assert artifact_domain(_card("4321", title="Card")) == "card"
    assert artifact_domain(_doc("Design", "body")) == "doc"
    assert (
        artifact_domain(_artifact("commit", source_type="git_metadata", external_id="a" * 40))
        == "commit"
    )


async def test_cross_domain_pair_with_token_overlap_is_a_candidate() -> None:
    code = _code_file("src/payment_service.py")
    card = _card("4321", title="payment service rollout", body_text="ship payment service")
    candidates = await generate_candidates([code, card], existing_pairs=NO_PAIRS)
    pairs = {frozenset((c.from_artifact_id, c.to_artifact_id)) for c in candidates}
    assert frozenset((code.artifact_id, card.artifact_id)) in pairs
    # every candidate records at least one firing signal with a score.
    for cand in candidates:
        assert cand.signals
        assert all(0.0 <= score <= 1.0 for score in cand.signals.values())


async def test_section_proximity_fires_when_doc_names_the_path() -> None:
    code = _code_file("src/widget.py")
    doc = _doc("design", "The widget lives in src/widget.py and is core.")
    candidates = await generate_candidates([doc, code], existing_pairs=NO_PAIRS)
    by_pair = {(c.from_artifact_id, c.to_artifact_id): c.signals for c in candidates}
    assert (doc.artifact_id, code.artifact_id) in by_pair
    assert "section_proximity" in by_pair[(doc.artifact_id, code.artifact_id)]


async def test_same_domain_pairs_are_not_candidates() -> None:
    a = _code_file("src/alpha_service.py")
    b = _code_file("src/alpha_service_helper.py")  # same domain (code) + same dir + token overlap
    candidates = await generate_candidates([a, b], existing_pairs=NO_PAIRS)
    assert candidates == []


async def test_already_linked_pair_is_excluded() -> None:
    code = _code_file("src/payment_service.py")
    card = _card("4321", title="payment service", body_text="payment service")
    existing = frozenset({frozenset((code.artifact_id, card.artifact_id))})
    candidates = await generate_candidates([code, card], existing_pairs=existing)
    assert candidates == []


async def test_embedding_signal_is_none_safe() -> None:
    code = _code_file("src/standalone.py")
    card = _card("99", title="unrelated card", body_text="nothing in common")
    # No textual signal fires and no provider ⇒ no candidate (and no crash).
    candidates = await generate_candidates([code, card], existing_pairs=NO_PAIRS, similarity=None)
    assert candidates == []


async def test_embedding_signal_fires_via_provider() -> None:
    code = _code_file("src/standalone.py")
    card = _card("99", title="zzz", body_text="qqq")  # no textual overlap with the file
    provider: SimilarityProvider = _StubProvider(
        {code.artifact_id: [ScoredArtifact(artifact_id=card.artifact_id, similarity=0.91)]}
    )
    candidates = await generate_candidates(
        [code, card], existing_pairs=NO_PAIRS, similarity=provider
    )
    by_pair = {(c.from_artifact_id, c.to_artifact_id): c.signals for c in candidates}
    assert (code.artifact_id, card.artifact_id) in by_pair
    assert by_pair[(code.artifact_id, card.artifact_id)]["embedding_similarity"] == pytest.approx(
        0.91
    )


async def test_fan_out_is_bounded_per_from_artifact() -> None:
    # one card that token-overlaps with many code files: without a bound it would
    # produce a candidate to every file; the generator must cap it at K.
    shared = "payment service rollout module"
    card = _card("4321", title=shared, body_text=shared)
    files = [_code_file(f"src/payment_service_{i}.py") for i in range(CANDIDATE_FAN_OUT_K + 8)]
    candidates = await generate_candidates([card, *files], existing_pairs=NO_PAIRS)
    from_card = [c for c in candidates if c.from_artifact_id == card.artifact_id]
    assert len(from_card) <= CANDIDATE_FAN_OUT_K


async def test_generator_is_deterministic_for_fixed_inputs() -> None:
    card = _card("4321", title="payment service rollout", body_text="ship the payment service")
    files = [_code_file(f"src/payment_service_{i}.py") for i in range(20)]
    artifacts = [card, *files]
    first = await generate_candidates(artifacts, existing_pairs=NO_PAIRS)
    second = await generate_candidates(artifacts, existing_pairs=NO_PAIRS)
    as_tuples = lambda cs: [  # noqa: E731
        (c.from_artifact_id, c.to_artifact_id, tuple(sorted(c.signals.items()))) for c in cs
    ]
    assert as_tuples(first) == as_tuples(second)
