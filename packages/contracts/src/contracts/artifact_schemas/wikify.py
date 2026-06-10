"""Wikify output shapes: chunks, summaries, concepts, source-backed facts.

Summaries and concepts are *interpreted* knowledge (architecture §5): they must
be marked knowledge_kind="interpreted" and ranked below source-backed evidence.
ARTIFACT_SCHEMA_VERSION is part of the chunk-summary cache key, so changing any
shape here must bump contracts.versions.OUTPUT_SCHEMA_VERSION.
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from contracts.artifact_schemas import ArtifactModel

WikifyArtifactType = Literal["chunk", "summary", "concept", "source_backed_fact"]
KnowledgeKind = Literal["interpreted", "source_backed"]

_KNOWLEDGE_KIND_BY_TYPE: dict[str, KnowledgeKind] = {
    "chunk": "source_backed",
    "summary": "interpreted",
    "concept": "interpreted",
    "source_backed_fact": "source_backed",
}


class Chunk(ArtifactModel):
    """One deterministic chunk of normalized source text."""

    index: int = Field(ge=0)
    text: str = Field(min_length=1)
    chunk_hash: str


class ConceptDraft(ArtifactModel):
    """A named concept the model extracted; interpreted knowledge."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class SourceBackedFactDraft(ArtifactModel):
    """A fact with a verbatim supporting quote from the source text."""

    statement: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class WikifyGeneration(ArtifactModel):
    """The ModelClient response shape for one source's chunks."""

    summary: str = Field(min_length=1)
    concepts: tuple[ConceptDraft, ...] = ()
    facts: tuple[SourceBackedFactDraft, ...] = ()


class WikifyArtifactDraft(ArtifactModel):
    """One knowledge_artifact row to be written by the wikify pipeline."""

    artifact_type: WikifyArtifactType
    knowledge_kind: KnowledgeKind
    title: str | None = None
    body_text: str = Field(min_length=1)
    authority_score: float = Field(ge=0.0, le=1.0)
    freshness_score: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _kind_matches_type(self) -> Self:
        expected = _KNOWLEDGE_KIND_BY_TYPE[self.artifact_type]
        if self.knowledge_kind != expected:
            raise ValueError(
                f"artifact_type {self.artifact_type!r} requires "
                f"knowledge_kind {expected!r}, got {self.knowledge_kind!r}"
            )
        return self


__all__ = [
    "Chunk",
    "ConceptDraft",
    "KnowledgeKind",
    "SourceBackedFactDraft",
    "WikifyArtifactDraft",
    "WikifyArtifactType",
    "WikifyGeneration",
]
