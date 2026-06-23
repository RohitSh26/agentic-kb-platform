"""relationship_judgment_cache table.

The phase-3B LLM judge rules on candidate pairs from relationship_candidate. Every
judge call is GATED by this cache (architecture invariant 4): a hit returns the
stored verdict and makes ZERO LLM calls, exactly like generation_cache /
embedding_cache.

Cache key (composite PK): (hash_a, hash_b, relation_schema_version, prompt_version,
model_version). hash_a/hash_b are the two endpoints' content hashes, SORTED at write
time so the key is direction-independent — one row per unordered pair under a fixed
schema/prompt/model. Bumping relation_schema_version / prompt_version / model_version
re-judges affected pairs (a new key ⇒ a miss ⇒ a fresh judgment).

Idempotency: the composite PK + on-conflict-do-nothing make a rebuild a no-op (no
duplicate rows). This is a build-plane gate/audit artifact — NOT served through MCP,
so it carries NO membership columns.

Backfill: none — new table.

Downgrade drops the table and its index — a clean reversal (no data the served KB
depends on; cache rows are recomputable from source + prompt + model). Verified
up -> down -> up.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relationship_judgment_cache",
        # composite cache key parts (PK), kept as columns so the key is auditable.
        sa.Column("hash_a", sa.Text(), nullable=False),
        sa.Column("hash_b", sa.Text(), nullable=False),
        sa.Column("relation_schema_version", sa.Integer(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        # the judge's verdict.
        sa.Column("relation_type", sa.Text(), nullable=False),
        sa.Column("trust_bucket", sa.Text(), nullable=False),
        sa.Column("supporting_quote", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "hash_a",
            "hash_b",
            "relation_schema_version",
            "prompt_version",
            "model_version",
            name="pk_relationship_judgment_cache",
        ),
    )
    op.create_index(
        "ix_relationship_judgment_cache_trust_bucket",
        "relationship_judgment_cache",
        ["trust_bucket"],
    )
    # Idempotency for phase-3B judge edges: one row per logical (from, to,
    # edge_type) judged link, mirroring uq_knowledge_edge_linker. A rebuild
    # refreshes the row in place instead of accreting a duplicate per build.
    op.create_index(
        "uq_knowledge_edge_judge",
        "knowledge_edge",
        ["from_artifact_id", "to_artifact_id", "edge_type"],
        unique=True,
        postgresql_where=sa.text("source = 'llm_judge'"),
    )


def downgrade() -> None:
    op.drop_index("uq_knowledge_edge_judge", table_name="knowledge_edge")
    op.drop_index(
        "ix_relationship_judgment_cache_trust_bucket",
        table_name="relationship_judgment_cache",
    )
    op.drop_table("relationship_judgment_cache")
