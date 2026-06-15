from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class EntailmentCache(Base):
    """Caches the L3 verifier's LLM-entailment verdict so an unchanged claim is
    NEVER re-checked (architecture invariant 4; gates the model exactly like
    generation_cache / embedding_cache / relationship_judgment_cache).

    kb-builder OWNS the Postgres schema, so this table's model + migration live
    here even though the L3 verifier that reads/writes it runs in mcp-server.
    mcp-server NEVER runs migrations and reaches the table only via raw SQL (same
    pattern as retrieval_event); this ORM model is the build-plane's source of
    truth for the table shape and keeps migration 0015 honest.

    Cache key (PK): (claim_hash, evidence_ids_hash, prompt_version, model_version).
    ``claim_hash`` is a stable hash of the claim text; ``evidence_ids_hash`` a
    stable hash over the SORTED cited, resolvable evidence ids the entailment ran
    against. Bumping prompt_version / model_version re-runs affected claims (a new
    key => a miss => a fresh entailment). A hit returns the stored verdict with
    ZERO LLM calls.

    The verdict is a single ``entailed`` bool plus a terse ``reason`` — NO answer
    or evidence text is stored (only the hashed key, the bool, and the reason).

    Idempotency: the PK + on-conflict-do-nothing make a re-verify a no-op (no
    duplicate rows). This is a verifier-plane audit/gate artifact — NOT served
    through MCP, so it carries no membership/ACL columns.
    """

    __tablename__ = "entailment_cache"

    # composite-key parts kept as columns so the key is inspectable/auditable.
    claim_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    evidence_ids_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    prompt_version: Mapped[str] = mapped_column(Text, primary_key=True)
    model_version: Mapped[str] = mapped_column(Text, primary_key=True)
    # the entailment verdict (no answer/evidence text — only the bool + reason).
    entailed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("ix_entailment_cache_entailed", "entailed"),)


__all__ = ["EntailmentCache"]
