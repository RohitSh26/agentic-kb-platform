import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class GenerationCacheArtifact(Base):
    """Maps one generation_cache row to its N output artifacts, in order.

    This table is the single source of truth on cache hits;
    generation_cache.output_artifact_id is a denormalized copy of position 0
    kept only to match the architecture §6 sketch — never read it on the hit
    path. artifact_id has no ON DELETE action on purpose: deleting an artifact
    still referenced here must fail rather than silently shrink a cached
    output set.
    """

    __tablename__ = "generation_cache_artifact"

    cache_key: Mapped[str] = mapped_column(
        Text,
        ForeignKey("generation_cache.cache_key", ondelete="CASCADE"),
        primary_key=True,
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_artifact.artifact_id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "cache_key", "position", name="uq_generation_cache_artifact_cache_key_position"
        ),
    )
