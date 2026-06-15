"""Canonical Knowledge Registry tables (architecture §6)."""

from agentic_kb_builder.infrastructure.postgres.models.base import Base
from agentic_kb_builder.infrastructure.postgres.models.embedding_cache import EmbeddingCache
from agentic_kb_builder.infrastructure.postgres.models.generation_cache import GenerationCache
from agentic_kb_builder.infrastructure.postgres.models.generation_cache_artifact import (
    GenerationCacheArtifact,
)
from agentic_kb_builder.infrastructure.postgres.models.kb_build_run import KbBuildRun
from agentic_kb_builder.infrastructure.postgres.models.knowledge_artifact import KnowledgeArtifact
from agentic_kb_builder.infrastructure.postgres.models.knowledge_edge import KnowledgeEdge
from agentic_kb_builder.infrastructure.postgres.models.relationship_candidate import (
    RelationshipCandidate,
)
from agentic_kb_builder.infrastructure.postgres.models.retrieval_event import RetrievalEvent
from agentic_kb_builder.infrastructure.postgres.models.source_item import SourceItem

__all__ = [
    "Base",
    "EmbeddingCache",
    "GenerationCache",
    "GenerationCacheArtifact",
    "KbBuildRun",
    "KnowledgeArtifact",
    "KnowledgeEdge",
    "RelationshipCandidate",
    "RetrievalEvent",
    "SourceItem",
]
