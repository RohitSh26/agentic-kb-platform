import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class RelationshipCandidate(Base):
    """A cross-domain artifact pair the cheap generator flagged for the phase-3B
    LLM judge (docs/contracts/relationship-candidates.md).

    AUDIT / MEASUREMENT artifact ONLY — never served through MCP, so it carries
    NO membership columns (no valid_from_seq / invalidated_at_seq). It is not part
    of the served KB; mcp-server never reads it. kb_version is a logging label only.
    A candidate is NOT an edge: it has no edge_type and no trust_class and cannot
    support a claim.
    """

    __tablename__ = "relationship_candidate"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    from_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_artifact.artifact_id"), nullable=False
    )
    to_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_artifact.artifact_id"), nullable=False
    )
    # which signals fired + their scores: {signal_name: score, ...}
    signals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # coarse audit bucket: high / medium / low.
    candidate_recall_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    # the build label that generated the candidate — logging only, NOT membership.
    kb_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "candidate_recall_bucket IN ('high', 'medium', 'low')",
            name="candidate_recall_bucket",
        ),
        Index("ix_relationship_candidate_from_artifact_id", "from_artifact_id"),
        Index("ix_relationship_candidate_to_artifact_id", "to_artifact_id"),
        Index("ix_relationship_candidate_kb_version", "kb_version"),
        Index(
            "uq_relationship_candidate_pair_version",
            "from_artifact_id",
            "to_artifact_id",
            "kb_version",
            unique=True,
        ),
    )
