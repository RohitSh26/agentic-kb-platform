"""Crash-durable model-output cache (ADR-0027 / PR-35).

The nightly build commits once at the very end, and the paid-work memo tables
(generation_cache, embedding_cache) are written into that same transaction, so a
mid-build crash rolls them back and the re-run re-pays for every LLM/embedding call.

These two tables store the RAW model output keyed only by content + model identity,
with NO FK into build-scoped artifacts, so a side-committing writer can persist them
the moment the model returns. A re-run after a crash re-maps the cached output into a
fresh build_seq with zero model calls, while atomic activation (ADR-0013) is unchanged.

- doc_extraction_output: the serialized DocExtractionResult, keyed by the exact
  doc_extract_cache_key composition (content_hash + prompt + model + params + schema).
- embedding_output: the vector, keyed by (text_hash, embedding_model) only (the vector
  is a pure function of text + model; the artifact-scoped embedding_cache row stays for
  in-build replay — two keys by design).

Both are pure derived data: never served through MCP, no membership/ACL/provenance,
recomputable by paying the model again. Idempotent inserts (on-conflict-do-nothing).

Backfill: none — new tables. Downgrade drops them (cache rows are recomputable, nothing
served depends on them). Verified up -> down -> up.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "doc_extraction_output",
        sa.Column("cache_key", sa.Text(), nullable=False),
        # identity inputs kept as columns so the key is auditable / prunable by (model, prompt).
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_params_hash", sa.Text(), nullable=False),
        sa.Column("output_schema_version", sa.Text(), nullable=False),
        sa.Column("output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("cache_key", name="pk_doc_extraction_output"),
    )
    op.create_table(
        "embedding_output",
        sa.Column("text_hash", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_hash", sa.Text(), nullable=False),
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("text_hash", "embedding_model", name="pk_embedding_output"),
    )


def downgrade() -> None:
    op.drop_table("embedding_output")
    op.drop_table("doc_extraction_output")
