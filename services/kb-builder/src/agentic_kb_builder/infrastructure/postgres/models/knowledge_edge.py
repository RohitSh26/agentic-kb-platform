import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class KnowledgeEdge(Base):
    __tablename__ = "knowledge_edge"

    edge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    from_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_artifact.artifact_id"), nullable=False
    )
    to_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_artifact.artifact_id"), nullable=False
    )
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(Text)
    kb_version: Mapped[str] = mapped_column(Text, nullable=False)
    # Trust bucket (docs/contracts/trust-buckets.md). Deterministic producers
    # may only ever assign 'EXTRACTED'; the broker enforces the bucket at read
    # time via trust_floor. Server default backfills existing rows.
    trust_class: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'EXTRACTED'")
    )
    # Relation ontology version this edge was produced under
    # (docs/contracts/relation-ontology.md). Part of the relationship-judgment
    # cache key; server default backfills existing rows to version 1.
    relation_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    # Deterministic evidence pointer: the matched reference / match key /
    # changed-file path (relation-ontology.md "Required edge fields"). Nullable
    # so pre-existing graphify/linker edges are untouched; the cross-domain
    # linker populates it for every edge it writes.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_knowledge_edge_edge_type", "edge_type"),
        Index("ix_knowledge_edge_kb_version", "kb_version"),
        Index("ix_knowledge_edge_kb_version_trust_class", "kb_version", "trust_class"),
        Index("ix_knowledge_edge_from_artifact_id", "from_artifact_id"),
        Index("ix_knowledge_edge_to_artifact_id", "to_artifact_id"),
        Index(
            "uq_knowledge_edge_linker",
            "from_artifact_id",
            "to_artifact_id",
            "edge_type",
            unique=True,
            postgresql_where=text("source = 'linker'"),
        ),
    )
