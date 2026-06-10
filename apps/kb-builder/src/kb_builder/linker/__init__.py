"""Linker: connects Wikify knowledge to Graphify code with confidence-scored edges."""

from kb_builder.linker.deterministic import (
    DOC_LINK_CONFIDENCE,
    IMPLEMENTS_CONFIDENCE,
    find_deterministic_links,
)
from kb_builder.linker.records import LinkableArtifact
from kb_builder.linker.run import run_linker
from kb_builder.linker.semantic import (
    SEMANTIC_ACCEPT_THRESHOLD,
    ScoredArtifact,
    SimilarityProvider,
    find_semantic_links,
)
from kb_builder.linker.write_edges import (
    EDGE_SOURCE,
    LOW_CONFIDENCE_THRESHOLD,
    write_link_edges,
)

__all__ = [
    "DOC_LINK_CONFIDENCE",
    "EDGE_SOURCE",
    "IMPLEMENTS_CONFIDENCE",
    "LOW_CONFIDENCE_THRESHOLD",
    "SEMANTIC_ACCEPT_THRESHOLD",
    "LinkableArtifact",
    "ScoredArtifact",
    "SimilarityProvider",
    "find_deterministic_links",
    "find_semantic_links",
    "run_linker",
    "write_link_edges",
]
