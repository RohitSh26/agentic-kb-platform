from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentic_kb_builder.infrastructure.postgres.models.base import Base


class DocExtractionOutput(Base):
    """Crash-durable cache of one document's RAW LLM extraction output.

    The artifact-coupled ``generation_cache`` replays mapped artifact rows, but it is
    committed only in the build's single end-transaction, so a mid-build crash rolls it
    back and the re-run re-pays for the LLM call. This table stores the *model output
    itself* (the serialized ``DocExtractionResult``) keyed only by content + model
    identity, with NO FK into build-scoped artifacts, so it can be side-committed the
    moment the extractor returns and survives a build rollback. A re-run finds the cached
    output and re-maps it into a fresh build_seq with ZERO LLM calls.

    Key (PK): ``cache_key`` — the exact ``doc_extract_cache_key`` composition
    (content_hash + prompt_version + model_name + model_params_hash + output_schema_version).
    The identity columns are kept alongside for auditing/pruning; a prompt/model/schema bump
    yields a new key (a miss), so stale output is never replayed under new semantics.

    Pure derived data: never served through MCP, carries no membership/ACL/provenance — it
    only gates a model call. Recomputable by paying the model again. Idempotent insert
    (on-conflict-do-nothing) so a retry never duplicates or overwrites.
    """

    __tablename__ = "doc_extraction_output"

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    # identity inputs kept as columns so the key is auditable and prunable by (model, prompt).
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_params_hash: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    # the serialized DocExtractionResult (artifacts-only); re-mapped into rows on a hit.
    output_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


__all__ = ["DocExtractionOutput"]
