"""Wikify pipeline unit tests (PR-05): chunker determinism, draft assembly,
interpreted-knowledge marking. No database required; the cache-gating DB tests
live in test_build_engine.py."""

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from common.hashing import content_hash
from contracts.artifact_schemas import (
    Chunk,
    ConceptDraft,
    NormalizedContent,
    SourceBackedFactDraft,
    SourceRef,
    WikifyArtifactDraft,
    WikifyGeneration,
)
from kb_builder.wikify import MAX_CHUNK_CHARS, WikifyGenerator, chunk_text
from kb_builder.wikify.generate import (
    CHUNK_AUTHORITY,
    CONCEPT_AUTHORITY,
    FACT_AUTHORITY,
    SUMMARY_AUTHORITY,
)


def test_chunker_is_deterministic() -> None:
    text = "para one\n\npara two\n\n" + ("long paragraph " * 400) + "\n\npara four"
    first = chunk_text(text)
    second = chunk_text(text)
    assert first == second
    assert [chunk.index for chunk in first] == list(range(len(first)))
    for chunk in first:
        assert chunk.chunk_hash == content_hash(chunk.text)


def test_chunker_packs_paragraphs_and_splits_oversized_ones() -> None:
    small = chunk_text("a\n\nb\n\nc")
    assert len(small) == 1
    assert small[0].text == "a\n\nb\n\nc"

    oversized = "x" * (MAX_CHUNK_CHARS * 2 + 10)
    chunks = chunk_text(oversized)
    assert len(chunks) == 3
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == oversized

    assert chunk_text("") == []
    assert chunk_text("\n\n\n\n") == []

    # a paragraph of exactly MAX_CHUNK_CHARS fills one chunk without splitting
    exact = "y" * MAX_CHUNK_CHARS
    exact_chunks = chunk_text(exact + "\n\n" + "tail")
    assert [chunk.text for chunk in exact_chunks] == [exact, "tail"]


def test_draft_kind_must_match_type() -> None:
    with pytest.raises(ValidationError, match="interpreted"):
        WikifyArtifactDraft(
            artifact_type="summary",
            knowledge_kind="source_backed",
            body_text="x",
            authority_score=0.5,
            freshness_score=1.0,
        )
    with pytest.raises(ValidationError, match="source_backed"):
        WikifyArtifactDraft(
            artifact_type="chunk",
            knowledge_kind="interpreted",
            body_text="x",
            authority_score=1.0,
            freshness_score=1.0,
        )


class FakeModelClient:
    model_name = "gpt-test"
    model_params_hash = "params-test"

    def __init__(self, generation: WikifyGeneration) -> None:
        self.calls = 0
        self._generation = generation

    async def generate_wikify(
        self, *, chunks: Sequence[Chunk], prompt_version: str
    ) -> WikifyGeneration:
        self.calls += 1
        return self._generation


CONTENT = NormalizedContent(
    source=SourceRef(
        source_type="github_doc",
        source_uri="https://github.com/o/r/blob/sha1/README.md",
        source_version="sha1",
        repo="o/r",
        path="README.md",
    ),
    text="The service stores users.\n\nIt exposes a REST API.",
    content_hash=content_hash("The service stores users.\n\nIt exposes a REST API."),
)


async def test_generator_marks_interpreted_below_source_backed() -> None:
    generation = WikifyGeneration(
        summary="A user service with a REST API.",
        concepts=(ConceptDraft(name="User service", description="Stores users."),),
        facts=(SourceBackedFactDraft(statement="It has a REST API", quote="REST API"),),
    )
    generator = WikifyGenerator(FakeModelClient(generation))
    assert generator.model_name == "gpt-test"
    assert generator.model_params_hash == "params-test"

    drafts = await generator.wikify(CONTENT)
    by_type = {draft.artifact_type: draft for draft in drafts}
    assert sorted(by_type) == ["chunk", "concept", "source_backed_fact", "summary"]

    assert by_type["summary"].knowledge_kind == "interpreted"
    assert by_type["concept"].knowledge_kind == "interpreted"
    assert by_type["chunk"].knowledge_kind == "source_backed"
    assert by_type["source_backed_fact"].knowledge_kind == "source_backed"
    # interpreted knowledge must sit strictly below source-backed evidence
    interpreted_max = max(SUMMARY_AUTHORITY, CONCEPT_AUTHORITY)
    source_backed_min = min(CHUNK_AUTHORITY, FACT_AUTHORITY)
    assert interpreted_max < source_backed_min
    assert by_type["summary"].authority_score == SUMMARY_AUTHORITY
    assert by_type["chunk"].authority_score == CHUNK_AUTHORITY
    assert by_type["source_backed_fact"].title is None
    assert "REST API" in by_type["source_backed_fact"].body_text


async def test_generator_drops_facts_whose_quote_is_not_in_source() -> None:
    generation = WikifyGeneration(
        summary="A summary.",
        facts=(
            SourceBackedFactDraft(statement="real", quote="REST API"),
            SourceBackedFactDraft(statement="invented", quote="a graph database"),
        ),
    )
    drafts = await WikifyGenerator(FakeModelClient(generation)).wikify(CONTENT)
    facts = [draft for draft in drafts if draft.artifact_type == "source_backed_fact"]
    assert len(facts) == 1
    assert "real" in facts[0].body_text
    assert all("invented" not in draft.body_text for draft in drafts)
