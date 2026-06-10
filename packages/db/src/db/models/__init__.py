"""Canonical Knowledge Registry tables (architecture §6)."""

from db.models.base import Base
from db.models.embedding_cache import EmbeddingCache
from db.models.generation_cache import GenerationCache
from db.models.kb_build_run import KbBuildRun
from db.models.knowledge_artifact import KnowledgeArtifact
from db.models.knowledge_edge import KnowledgeEdge
from db.models.retrieval_event import RetrievalEvent
from db.models.source_item import SourceItem

__all__ = [
    "Base",
    "EmbeddingCache",
    "GenerationCache",
    "KbBuildRun",
    "KnowledgeArtifact",
    "KnowledgeEdge",
    "RetrievalEvent",
    "SourceItem",
]
