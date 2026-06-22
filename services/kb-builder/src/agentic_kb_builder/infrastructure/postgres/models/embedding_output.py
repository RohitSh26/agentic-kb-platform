from datetime import datetime

from sqlalchemy import DateTime, Float, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class EmbeddingOutput(Base):
    """Crash-durable cache of one embedding vector (ADR-0027).

    The artifact-scoped ``embedding_cache`` ((artifact_id, text_hash, model)) is committed
    only in the build's single end-transaction, so a mid-build crash rolls it back and the
    re-run re-embeds. This table stores the vector keyed by ``(text_hash, embedding_model)``
    ONLY — the vector is a pure function of text + model, independent of any artifact — so it
    can be side-committed when the embedder returns and survives a build rollback. A re-run
    reuses the cached vector with ZERO embedding calls and still records its own artifact-scoped
    ``embedding_cache`` row for in-build replay (two keys by design).

    Pure derived data: never served, no membership/ACL/provenance; only gates the embedder.
    Idempotent insert (on-conflict-do-nothing).
    """

    __tablename__ = "embedding_output"

    text_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding_model: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(ARRAY(Float()), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


__all__ = ["EmbeddingOutput"]
