"""Linker edge shapes: Wikify knowledge connected to Graphify code.

Direction convention is subject-verb-object, matching graphify edges
("test tests symbol", "symbol exposed_as endpoint"):

- ``doc-artifact documents concept`` — a wiki/doc-derived artifact documents a concept.
- ``card-artifact requests concept`` — an ADO-card-derived artifact requests a concept.
- ``code_symbol implements concept`` — a code symbol implements a concept.
- ``doc-artifact mentions code-artifact`` — a non-code artifact references a code
  file, symbol, or endpoint.

Note: architecture §5 sketches the chain as ``Concept → documents → Wiki``; the
stored direction is the SVO reading above. graph.get_neighbors consumers must
rely on this docstring, not the sketch arrows. ``exposed_as``/``tests`` edges are
emitted by graphify, never duplicated by the linker.
"""

import uuid
from typing import Literal

from pydantic import Field

from contracts.artifact_schemas import ArtifactModel

LinkerEdgeType = Literal["documents", "implements", "requests", "mentions"]
LinkStrategy = Literal["deterministic", "semantic"]


class LinkEdgeDraft(ArtifactModel):
    """One linker-produced knowledge_edge row (source='linker')."""

    from_artifact_id: uuid.UUID
    to_artifact_id: uuid.UUID
    edge_type: LinkerEdgeType
    confidence: float = Field(ge=0.0, le=1.0)
    strategy: LinkStrategy


__all__ = ["LinkEdgeDraft", "LinkStrategy", "LinkerEdgeType"]
