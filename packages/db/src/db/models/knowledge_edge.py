import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_knowledge_edge_edge_type", "edge_type"),
        Index("ix_knowledge_edge_kb_version", "kb_version"),
        Index("ix_knowledge_edge_from_artifact_id", "from_artifact_id"),
        Index("ix_knowledge_edge_to_artifact_id", "to_artifact_id"),
        Index(
            "uq_knowledge_edge_linker",
            "from_artifact_id",
            "to_artifact_id",
            "edge_type",
            "kb_version",
            unique=True,
            postgresql_where=text("source = 'linker'"),
        ),
    )
