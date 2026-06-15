from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class RelationshipJudgmentCache(Base):
    """Caches the phase-3B LLM judge's verdict on a candidate pair so an unchanged
    pair is NEVER re-judged (architecture invariant 4; gates the model exactly like
    generation_cache / embedding_cache).

    Cache key (PK): (hash_a, hash_b, relation_schema_version, prompt_version,
    model_version). ``hash_a``/``hash_b`` are the two endpoints' content hashes,
    SORTED so the key is direction-independent — the same unordered pair under the
    same schema/prompt/model resolves to one row. A hit returns the stored verdict
    with ZERO LLM calls.

    Idempotency: the cache_key PK + on-conflict-do-nothing make a rebuild a no-op
    (no duplicate rows). This is a build-plane audit/gate artifact — NOT served
    through MCP, so it carries no membership columns.
    """

    __tablename__ = "relationship_judgment_cache"

    # composite-key parts kept as columns so the key is inspectable/auditable.
    hash_a: Mapped[str] = mapped_column(Text, primary_key=True)
    hash_b: Mapped[str] = mapped_column(Text, primary_key=True)
    relation_schema_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt_version: Mapped[str] = mapped_column(Text, primary_key=True)
    model_version: Mapped[str] = mapped_column(Text, primary_key=True)
    # the judge's verdict (verbatim ontology relation + trust bucket + quote + reason).
    relation_type: Mapped[str] = mapped_column(Text, nullable=False)
    trust_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_quote: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("ix_relationship_judgment_cache_trust_bucket", "trust_bucket"),)


__all__ = ["RelationshipJudgmentCache"]
