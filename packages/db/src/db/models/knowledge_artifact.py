import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class KnowledgeArtifact(Base):
    __tablename__ = "knowledge_artifact"

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_item.source_id"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    artifact_hash: Mapped[str | None] = mapped_column(Text)
    kb_version: Mapped[str] = mapped_column(Text, nullable=False)
    # "interpreted" (summaries/concepts) vs "source_backed"; interpreted knowledge
    # must rank below source-backed evidence (architecture §5).
    knowledge_kind: Mapped[str | None] = mapped_column(Text)
    authority_score: Mapped[float | None] = mapped_column(Float)
    freshness_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    __table_args__ = (
        # the ck naming convention expands this to ck_knowledge_artifact_knowledge_kind
        CheckConstraint(
            "knowledge_kind IS NULL OR knowledge_kind IN ('interpreted', 'source_backed')",
            name="knowledge_kind",
        ),
        Index("ix_knowledge_artifact_content_hash", "content_hash"),
        Index("ix_knowledge_artifact_kb_version", "kb_version"),
        Index("ix_knowledge_artifact_source_id", "source_id"),
    )
