"""Docify output shapes: the artifacts Graphify's LLM doc extraction maps to.

ADR-0023 retires the hand-rolled wikify prose pipeline and routes document sources
(github_doc / azure_wiki / ado_card) through Graphify's LLM doc extractor. The mapper
(docify.docify_backend.map_doc_extraction) re-derives our trust contract from Graphify's
raw output rather than copying its labels:

- ``summary`` / ``concept`` artifacts are *interpreted* knowledge (architecture §5):
  marked knowledge_kind="interpreted", ranked below source-backed evidence.
- a ``source_backed`` artifact carries a verbatim quote that the broker's L0 verifier
  can confirm — emitted ONLY when the concept's supporting sentence is a verbatim
  substring of the source text.

Docify produces ARTIFACTS ONLY — no edges (parity with the wikify it replaces, which
wrote none). Graphify's concept->concept relations are generic relatedness, which the
relation ontology bans as an edge (docs/contracts/relation-ontology.md: "no generic
related_to ... it becomes a candidate ... never an edge"). Promoting them to candidates
for the phase-3 judge is a tracked follow-up (ADR-0023).

The artifact ROW shape is FROZEN identical to the retired wikify drafts (types,
knowledge_kind, authority/freshness scores, citable body) so the broker/verifier/Search
projection are unaffected and no Alembic migration is required (ADR-0023 §5).
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from agentic_kb_builder.domain.artifact_model import ArtifactModel

# The frozen artifact-row vocabulary (identical to the retired wikify types): a
# document yields an interpreted ``summary``; each concept yields either an
# interpreted ``concept`` or a verbatim-anchored ``source_backed_fact``.
DocArtifactType = Literal["summary", "concept", "source_backed_fact"]
DocKnowledgeKind = Literal["interpreted", "source_backed"]

_KNOWLEDGE_KIND_BY_TYPE: dict[str, DocKnowledgeKind] = {
    "summary": "interpreted",
    "concept": "interpreted",
    "source_backed_fact": "source_backed",
}


class DocArtifactDraft(ArtifactModel):
    """One knowledge_artifact row to be written by the docify pipeline.

    Field-identical to the retired ``WikifyArtifactDraft`` so the registry row shape is
    unchanged (ADR-0023 §5)."""

    artifact_type: DocArtifactType
    knowledge_kind: DocKnowledgeKind
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


class DocExtractionResult(ArtifactModel):
    """The normalized output of one document's Graphify LLM extraction (artifacts only)."""

    artifacts: tuple[DocArtifactDraft, ...] = ()


__all__ = [
    "DocArtifactDraft",
    "DocArtifactType",
    "DocExtractionResult",
    "DocKnowledgeKind",
]
