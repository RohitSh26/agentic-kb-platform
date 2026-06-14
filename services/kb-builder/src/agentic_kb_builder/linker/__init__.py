"""Linker: connects Wikify knowledge to Graphify code with confidence-scored edges."""

from agentic_kb_builder.linker.cross_domain import (
    find_cross_domain_links,
    find_doc_work_item_mentions,
    parse_sha_references,
    parse_work_item_references,
)
from agentic_kb_builder.linker.deterministic import (
    DOC_LINK_CONFIDENCE,
    IMPLEMENTS_CONFIDENCE,
    find_deterministic_links,
)
from agentic_kb_builder.linker.records import LinkableArtifact
from agentic_kb_builder.linker.run import run_linker
from agentic_kb_builder.linker.semantic import (
    SEMANTIC_ACCEPT_THRESHOLD,
    ScoredArtifact,
    SimilarityProvider,
    find_semantic_links,
)
from agentic_kb_builder.linker.write_edges import (
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
    "find_cross_domain_links",
    "find_deterministic_links",
    "find_doc_work_item_mentions",
    "find_semantic_links",
    "parse_sha_references",
    "parse_work_item_references",
    "run_linker",
    "write_link_edges",
]
