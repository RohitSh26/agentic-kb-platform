"""entailment_cache table (PR-31, ADR-0011 phase 4).

The L3 verifier (LLM entailment) in mcp-server runs ONLY for claims L0-L2 could
not adjudicate deterministically. Every L3 entailment call is GATED by this cache
(architecture invariant 4): a hit returns the stored verdict and makes ZERO LLM
calls, exactly like generation_cache / embedding_cache / relationship_judgment_cache.

kb-builder OWNS the Postgres schema, so the table is created here even though the
verifier that reads/writes it lives in mcp-server — mcp-server NEVER runs
migrations and reaches this table only through raw SQL (same pattern as
retrieval_event), never an ORM model crossing the service boundary.

Cache key (composite PK): (claim_hash, evidence_ids_hash, prompt_version,
model_version). ``claim_hash`` is a stable hash of the claim text; ``evidence_ids_hash``
is a stable hash over the SORTED set of cited, resolvable evidence ids the entailment
ran against. Bumping prompt_version / model_version re-runs affected claims (a new
key => a miss => a fresh entailment). The verdict is a single ``entailed`` bool plus a
short ``reason`` (no answer/evidence text is stored — only the hashed key, the bool,
and the model's terse reason string).

Idempotency: the composite PK + on-conflict-do-nothing make a re-verify a no-op
(no duplicate rows). This is a verifier-plane gate/audit artifact — NOT served
through MCP, so it carries NO membership/ACL columns.

Backfill: none — new table.

Downgrade drops the table and its index — a clean reversal (no data the served KB
depends on; cache rows are recomputable from claim + evidence + prompt + model).
Verified up -> down -> up.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entailment_cache",
        # composite cache key parts (PK), kept as columns so the key is auditable.
        sa.Column("claim_hash", sa.Text(), nullable=False),
        sa.Column("evidence_ids_hash", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        # the entailment verdict (no answer/evidence text — only the bool + reason).
        sa.Column("entailed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "claim_hash",
            "evidence_ids_hash",
            "prompt_version",
            "model_version",
            name="pk_entailment_cache",
        ),
    )
    op.create_index(
        "ix_entailment_cache_entailed",
        "entailment_cache",
        ["entailed"],
    )


def downgrade() -> None:
    op.drop_index("ix_entailment_cache_entailed", table_name="entailment_cache")
    op.drop_table("entailment_cache")
