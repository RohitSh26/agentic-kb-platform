"""Create the canonical Knowledge Registry tables (architecture §6).

Revision ID: 0001
Revises:
Create Date: 2026-06-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "source_item",
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index("ix_source_item_content_hash", "source_item", ["content_hash"])
    op.create_index(
        "ix_source_item_source_uri_source_version",
        "source_item",
        ["source_uri", "source_version"],
    )

    op.create_table(
        "knowledge_artifact",
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("artifact_type", sa.Text(), nullable=False),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "source_item.source_id", name="fk_knowledge_artifact_source_id_source_item"
            ),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("artifact_hash", sa.Text(), nullable=True),
        sa.Column("kb_version", sa.Text(), nullable=False),
        sa.Column("authority_score", sa.Float(), nullable=True),
        sa.Column("freshness_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index("ix_knowledge_artifact_content_hash", "knowledge_artifact", ["content_hash"])
    op.create_index("ix_knowledge_artifact_kb_version", "knowledge_artifact", ["kb_version"])
    op.create_index("ix_knowledge_artifact_source_id", "knowledge_artifact", ["source_id"])

    op.create_table(
        "knowledge_edge",
        sa.Column(
            "edge_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "from_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "knowledge_artifact.artifact_id",
                name="fk_knowledge_edge_from_artifact_id_knowledge_artifact",
            ),
            nullable=False,
        ),
        sa.Column(
            "to_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "knowledge_artifact.artifact_id",
                name="fk_knowledge_edge_to_artifact_id_knowledge_artifact",
            ),
            nullable=False,
        ),
        sa.Column("edge_type", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("kb_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index("ix_knowledge_edge_edge_type", "knowledge_edge", ["edge_type"])
    op.create_index("ix_knowledge_edge_kb_version", "knowledge_edge", ["kb_version"])
    op.create_index("ix_knowledge_edge_from_artifact_id", "knowledge_edge", ["from_artifact_id"])
    op.create_index("ix_knowledge_edge_to_artifact_id", "knowledge_edge", ["to_artifact_id"])

    op.create_table(
        "generation_cache",
        sa.Column("cache_key", sa.Text(), primary_key=True),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_params_hash", sa.Text(), nullable=False),
        sa.Column("output_schema_version", sa.Text(), nullable=False),
        sa.Column(
            "output_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "knowledge_artifact.artifact_id",
                name="fk_generation_cache_output_artifact_id_knowledge_artifact",
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index("ix_generation_cache_input_hash", "generation_cache", ["input_hash"])

    op.create_table(
        "embedding_cache",
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "knowledge_artifact.artifact_id",
                name="fk_embedding_cache_artifact_id_knowledge_artifact",
            ),
            primary_key=True,
        ),
        sa.Column("text_hash", sa.Text(), primary_key=True),
        sa.Column("embedding_model", sa.Text(), primary_key=True),
        sa.Column("embedding_hash", sa.Text(), nullable=False),
        sa.Column("azure_search_doc_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )

    op.create_table(
        "kb_build_run",
        sa.Column(
            "build_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kb_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sources_seen", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("sources_changed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("artifacts_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("artifacts_updated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("artifacts_deleted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("embedding_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "search_docs_upserted", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )
    op.create_index("ix_kb_build_run_kb_version", "kb_build_run", ["kb_version"])

    op.create_table(
        "retrieval_event",
        sa.Column(
            "retrieval_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("context_pack_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column("normalized_query", sa.Text(), nullable=True),
        sa.Column("retrieval_profile", sa.Text(), nullable=True),
        sa.Column("kb_version", sa.Text(), nullable=False),
        sa.Column("source_filters", postgresql.JSONB(), nullable=True),
        sa.Column(
            "returned_artifact_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True
        ),
        sa.Column(
            "reused_evidence_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True
        ),
        sa.Column(
            "new_evidence_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True
        ),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("semantic_reuse", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("tokens_returned", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index("ix_retrieval_event_run_id", "retrieval_event", ["run_id"])
    op.create_index("ix_retrieval_event_context_pack_id", "retrieval_event", ["context_pack_id"])
    op.create_index("ix_retrieval_event_normalized_query", "retrieval_event", ["normalized_query"])
    op.create_index("ix_retrieval_event_kb_version", "retrieval_event", ["kb_version"])


def downgrade() -> None:
    # Reverse dependency order: children before knowledge_artifact before source_item.
    op.drop_table("retrieval_event")
    op.drop_table("kb_build_run")
    op.drop_table("embedding_cache")
    op.drop_table("generation_cache")
    op.drop_table("knowledge_edge")
    op.drop_table("knowledge_artifact")
    op.drop_table("source_item")
