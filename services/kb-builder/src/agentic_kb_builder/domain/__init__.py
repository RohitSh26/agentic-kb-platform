"""Versioned schemas for build-plane knowledge artifacts.

Docify/Graphify/Linker outputs are validated against these models before they
are written to the Postgres Knowledge Registry. The registry-facing shapes are
documented for consumers in docs/contracts/postgres-knowledge-registry.md.
"""

from agentic_kb_builder.domain.artifact_model import ARTIFACT_SCHEMA_VERSION, ArtifactModel
from agentic_kb_builder.domain.docify_artifacts import (
    DocArtifactDraft,
    DocArtifactType,
    DocExtractionResult,
    DocKnowledgeKind,
)
from agentic_kb_builder.domain.graph_artifacts import (
    CodeArtifactDraft,
    CodeArtifactType,
    CodeEdgeDraft,
    CodeEdgeType,
    GraphifyResult,
    SymbolKind,
)
from agentic_kb_builder.domain.judge_records import (
    INFERRED_EDGE_BUCKETS,
    JUDGE_RELATION_TYPES,
    JUDGE_TRUST_BUCKETS,
    JudgeCandidate,
    JudgeEndpoint,
    JudgeRelationType,
    JudgeTrustBucket,
    RelationshipJudgment,
)
from agentic_kb_builder.domain.link_records import (
    LinkEdgeDraft,
    LinkerEdgeType,
    LinkStrategy,
)
from agentic_kb_builder.domain.source_config import (
    AdoCardSourceSpec,
    AuthRef,
    AzureWikiSourceSpec,
    GithubCodeSourceSpec,
    GithubDocSourceSpec,
    GitMetadataConfig,
    GlobError,
    PathFilter,
    PathSelectSpec,
    SourceConfig,
    SourceDefaults,
    SourceSpec,
)
from agentic_kb_builder.domain.source_records import (
    NormalizedContent,
    SourceRef,
    SourceType,
)

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "INFERRED_EDGE_BUCKETS",
    "JUDGE_RELATION_TYPES",
    "JUDGE_TRUST_BUCKETS",
    "AdoCardSourceSpec",
    "ArtifactModel",
    "AuthRef",
    "AzureWikiSourceSpec",
    "CodeArtifactDraft",
    "CodeArtifactType",
    "CodeEdgeDraft",
    "CodeEdgeType",
    "DocArtifactDraft",
    "DocArtifactType",
    "DocExtractionResult",
    "DocKnowledgeKind",
    "GitMetadataConfig",
    "GithubCodeSourceSpec",
    "GithubDocSourceSpec",
    "GlobError",
    "GraphifyResult",
    "JudgeCandidate",
    "JudgeEndpoint",
    "JudgeRelationType",
    "JudgeTrustBucket",
    "LinkEdgeDraft",
    "LinkStrategy",
    "LinkerEdgeType",
    "NormalizedContent",
    "PathFilter",
    "PathSelectSpec",
    "RelationshipJudgment",
    "SourceConfig",
    "SourceDefaults",
    "SourceRef",
    "SourceSpec",
    "SourceType",
    "SymbolKind",
]
