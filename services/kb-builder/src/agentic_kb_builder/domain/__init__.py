"""Versioned schemas for build-plane knowledge artifacts.

Wikify/Graphify/Linker outputs are validated against these models before they
are written to the Postgres Knowledge Registry. The registry-facing shapes are
documented for consumers in docs/contracts/postgres-knowledge-registry.md.
"""

from agentic_kb_builder.domain.artifact_model import ARTIFACT_SCHEMA_VERSION, ArtifactModel
from agentic_kb_builder.domain.graph_artifacts import (
    CodeArtifactDraft,
    CodeArtifactType,
    CodeEdgeDraft,
    CodeEdgeType,
    FileGraph,
    GraphifyResult,
    ParsedCall,
    ParsedEndpoint,
    ParsedImport,
    ParsedSymbol,
    ParsedTest,
    SymbolKind,
)
from agentic_kb_builder.domain.link_records import (
    LinkEdgeDraft,
    LinkerEdgeType,
    LinkStrategy,
)
from agentic_kb_builder.domain.source_records import (
    NormalizedContent,
    SourceRef,
    SourceType,
)
from agentic_kb_builder.domain.wiki_artifacts import (
    Chunk,
    ConceptDraft,
    KnowledgeKind,
    SourceBackedFactDraft,
    WikifyArtifactDraft,
    WikifyArtifactType,
    WikifyGeneration,
)

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ArtifactModel",
    "Chunk",
    "CodeArtifactDraft",
    "CodeArtifactType",
    "CodeEdgeDraft",
    "CodeEdgeType",
    "ConceptDraft",
    "FileGraph",
    "GraphifyResult",
    "KnowledgeKind",
    "LinkEdgeDraft",
    "LinkStrategy",
    "LinkerEdgeType",
    "NormalizedContent",
    "ParsedCall",
    "ParsedEndpoint",
    "ParsedImport",
    "ParsedSymbol",
    "ParsedTest",
    "SourceBackedFactDraft",
    "SourceRef",
    "SourceType",
    "SymbolKind",
    "WikifyArtifactDraft",
    "WikifyArtifactType",
    "WikifyGeneration",
]
