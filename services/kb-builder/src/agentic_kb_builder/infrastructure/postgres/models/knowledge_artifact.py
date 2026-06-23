import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


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
    # Interval membership (docs/contracts/version-membership.md, ADR-0013).
    # valid_from_seq = the build_seq that introduced the row (set once at creation);
    # invalidated_at_seq = the build_seq it left the KB in (NULL while live). A row
    # is a member of version S iff valid_from_seq <= S AND (invalidated_at_seq IS
    # NULL OR invalidated_at_seq > S). Setting invalidated_at_seq never removes the
    # row from prior versions (invariant 5).
    valid_from_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    invalidated_at_seq: Mapped[int | None] = mapped_column(BigInteger)
    # Rename link: on a deterministic rename (same content_hash / signature, new
    # path) the new artifact points at the invalidated old one so history survives.
    prior_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_artifact.artifact_id")
    )
    # "interpreted" (summaries/concepts) vs "source_backed"; interpreted knowledge
    # must rank below source-backed evidence (architecture §5).
    knowledge_kind: Mapped[str | None] = mapped_column(Text)
    authority_score: Mapped[float | None] = mapped_column(Float)
    freshness_score: Mapped[float | None] = mapped_column(Float)
    # Normalized [0,1] graph-centrality (PageRank over knowledge_edge), recomputed each build and
    # folded into the broker rank key as a transparent prior (ADR-0028). NULL/0 ⇒ no graph signal.
    centrality_score: Mapped[float | None] = mapped_column(Float)
    # 1-based inclusive line span for code artifacts (path comes via source_id ->
    # source_item.path); lets L2 evidence return precise snippets at a source version.
    span_start: Mapped[int | None] = mapped_column(Integer)
    span_end: Mapped[int | None] = mapped_column(Integer)
    # Deterministic retrieval surface for code_symbol artifacts (ADR-0018 Phase 2):
    # split-identifier words + docstring + signature + decorator + call + import names.
    # Populated by the AST pass (Python-only, zero-LLM). NULL for non-code artifacts.
    search_text: Mapped[str | None] = mapped_column(Text)
    # team-based ACL: empty = org-public; non-empty = visible only to requesters
    # whose team set intersects. Enforced by the mcp-server Context Broker (PR-13).
    acl_teams: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
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
        Index("ix_knowledge_artifact_membership", "valid_from_seq", "invalidated_at_seq"),
    )
