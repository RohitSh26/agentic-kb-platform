import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class KbBuildRun(Base):
    __tablename__ = "kb_build_run"

    build_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kb_version: Mapped[str] = mapped_column(Text, nullable=False)
    # Monotonic build sequence (docs/contracts/version-membership.md, ADR-0013):
    # assigned once at run start from the kb_build_seq SEQUENCE. The active build's
    # build_seq is the served interval-membership cutoff S. UNIQUE.
    build_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
    # Publish-gate bookkeeping (docs/contracts/publish-gates.md, PR-25).
    # extractor_failures backs the extractor-error-rate gate; allow_large_delta is
    # the symbol-count-delta override flag; failed_gate + gate_measured_value record
    # which gate blocked activation and its measured value (NULL on a clean publish).
    extractor_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    allow_large_delta: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    failed_gate: Mapped[str | None] = mapped_column(Text)
    gate_measured_value: Mapped[float | None] = mapped_column(Float)
    # Ledger-mining counters (migration 0022, ADR-0034, PR-43): populated once per
    # build from that build's LedgerMiningResult (alias/ledger_mining.py). Rolled
    # up by day into v_retrieval_health's ledger_mined / ledger_unresolved split
    # (migration 0023) — kb-builder never writes retrieval_event itself, so these
    # counters are the safe, build-owned surface the dashboard split reads instead
    # (docs/contracts/observability-dashboard.md "Mined-vs-unresolved split").
    ledger_mining_misses_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    ledger_mining_mined: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    ledger_mining_unresolved: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    __table_args__ = (
        Index("ix_kb_build_run_kb_version", "kb_version"),
        UniqueConstraint("build_seq", name="uq_kb_build_run_build_seq"),
        # at most one build run may be active at a time (invariant 5)
        Index(
            "uq_kb_build_run_single_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
