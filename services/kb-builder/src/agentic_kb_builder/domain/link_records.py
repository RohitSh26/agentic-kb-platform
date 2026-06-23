"""Linker edge shapes: document knowledge connected to Graphify code.

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

from agentic_kb_builder.domain.artifact_model import ArtifactModel

LinkerEdgeType = Literal["documents", "implements", "requests", "mentions"]
LinkStrategy = Literal["deterministic", "semantic"]


class LinkEdgeDraft(ArtifactModel):
    """One linker-produced knowledge_edge row (source='linker').

    ``evidence`` is the deterministic pointer the relation ontology requires
    (docs/contracts/relation-ontology.md "Required edge fields"): the matched
    work-item reference, the changed-file path, or the verbatim match key. Stored
    as a small JSON object in knowledge_edge.evidence. Optional only because the
    pre- doc/concept rules predate the column; the cross-domain rules always
    set it.
    """

    from_artifact_id: uuid.UUID
    to_artifact_id: uuid.UUID
    edge_type: LinkerEdgeType
    confidence: float = Field(ge=0.0, le=1.0)
    strategy: LinkStrategy
    evidence: dict[str, str] | None = None


__all__ = ["LinkEdgeDraft", "LinkStrategy", "LinkerEdgeType"]
