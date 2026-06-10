"""Versioned schemas for build-plane knowledge artifacts.

Wikify/Graphify/Linker outputs are validated against these models before they
are written to the Postgres Knowledge Registry.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from contracts.versions import OUTPUT_SCHEMA_VERSION

ARTIFACT_SCHEMA_VERSION = OUTPUT_SCHEMA_VERSION


class ArtifactModel(BaseModel):
    """Base for all knowledge artifact models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = ARTIFACT_SCHEMA_VERSION


# Imported after ArtifactModel is defined; submodules import it from this package.
from contracts.artifact_schemas.graphify import (  # noqa: E402
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
from contracts.artifact_schemas.sources import (  # noqa: E402
    NormalizedContent,
    SourceRef,
    SourceType,
)
from contracts.artifact_schemas.wikify import (  # noqa: E402
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
