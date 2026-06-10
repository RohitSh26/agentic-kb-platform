import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class RetrievalEvent(Base):
    __tablename__ = "retrieval_event"

    retrieval_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    context_pack_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    # broker outcome: approved / reused / denied / needs_human_approval
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'approved'"))
    query_text: Mapped[str | None] = mapped_column(Text)
    normalized_query: Mapped[str | None] = mapped_column(Text)
    retrieval_profile: Mapped[str | None] = mapped_column(Text)
    kb_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_filters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    returned_artifact_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    reused_evidence_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    new_evidence_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    semantic_reuse: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    tokens_returned: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_retrieval_event_run_id", "run_id"),
        Index("ix_retrieval_event_context_pack_id", "context_pack_id"),
        Index("ix_retrieval_event_normalized_query", "normalized_query"),
        Index("ix_retrieval_event_kb_version", "kb_version"),
    )
