import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class SourceItem(Base):
    __tablename__ = "source_item"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    source_version: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # team-based ACL: empty = org-public; non-empty = visible only to requesters
    # whose team set intersects. Enforced by the mcp-server Context Broker (PR-13).
    acl_teams: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
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
        Index("ix_source_item_content_hash", "content_hash"),
        Index("ix_source_item_source_uri_source_version", "source_uri", "source_version"),
        UniqueConstraint("source_type", "source_uri", name="uq_source_item_source_type_source_uri"),
    )
