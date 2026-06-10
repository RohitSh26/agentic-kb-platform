import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class KbBuildRun(Base):
    __tablename__ = "kb_build_run"

    build_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kb_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sources_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sources_changed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    artifacts_created: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    artifacts_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    artifacts_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    embedding_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    search_docs_upserted: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_summary: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_kb_build_run_kb_version", "kb_version"),)
