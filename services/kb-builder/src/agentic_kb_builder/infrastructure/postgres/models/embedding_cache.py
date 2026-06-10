import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_artifact.artifact_id"), primary_key=True
    )
    text_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding_model: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # The vector itself, so the Search index is rebuildable from Postgres
    # without re-embedding (invariant 4). Azure Search holds the searchable
    # copy; this is the canonical one.
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(Float()))
    azure_search_doc_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
