"""v_retrieval_health mined-vs-unresolved split (PR-43, ADR-0034).

Replaces `v_retrieval_health` (migration `0020`) to add three columns —
`ledger_mined`, `ledger_unresolved`, `ledger_mined_rate` — a day roll-up of
`kb_build_run`'s new `ledger_mining_*` counters (migration `0022`), LEFT JOINed
onto the existing per-day `retrieval_event` aggregate on `day`.

Why a `kb_build_run` roll-up and not a per-event join: kb-builder never writes
`retrieval_event` (mcp-server's ledger alone,
`docs/contracts/postgres-knowledge-registry.md`), and the aggregate-only ACL
posture forbids any view SQL from touching `query_text` / `normalized_query` —
the only column that could join an individual historical miss row to the alias
that later resolved it. Full rationale:
`docs/contracts/observability-dashboard.md` "Mined-vs-unresolved split".

Every other view (`v_token_economics`, `v_build_health`, `v_budget_adherence`) is
untouched. View-only migration: no table, no column, no data — downgrade drops
this view and recreates the ORIGINAL (migration 0020) definition verbatim, so a
downgrade loses only the three new columns, nothing else. Verified up -> down ->
up.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-07

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The ORIGINAL migration-0020 definition, copied verbatim (never import another
# migration's module — each revision is a frozen, self-contained snapshot). This
# is the downgrade target.
V_RETRIEVAL_HEALTH_V1 = """
CREATE VIEW v_retrieval_health AS
SELECT
    date_trunc('day', created_at)::date AS day,
    count(*) AS events,
    count(*) FILTER (WHERE status = 'approved') AS approved,
    count(*) FILTER (WHERE status = 'reused') AS reused,
    count(*) FILTER (WHERE status = 'denied') AS denied,
    count(*) FILTER (WHERE status = 'needs_human_approval') AS needs_human_approval,
    count(*) FILTER (WHERE status = 'error') AS errors,
    (count(*) FILTER (WHERE status = 'error'))::float / count(*) AS error_rate,
    CASE
        WHEN count(*) FILTER (WHERE status IN ('reused', 'approved')) = 0 THEN NULL
        ELSE (count(*) FILTER (WHERE status = 'reused'))::float
             / count(*) FILTER (WHERE status IN ('reused', 'approved'))
    END AS evidence_reuse_rate,
    CASE
        WHEN count(*) FILTER (WHERE tool_name = 'context.request_more'
                              AND status IN ('approved', 'reused')) = 0 THEN NULL
        ELSE (count(*) FILTER (WHERE tool_name = 'context.request_more'
                               AND status IN ('approved', 'reused')
                               AND semantic_reuse))::float
             / count(*) FILTER (WHERE tool_name = 'context.request_more'
                                AND status IN ('approved', 'reused'))
    END AS semantic_cache_hit_rate,
    (count(*) FILTER (WHERE cache_hit))::float / count(*) AS cache_hit_rate,
    count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved')
        AS kb_search_answered,
    count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved'
                     AND coalesce(cardinality(returned_artifact_ids), 0) <= 1)
        AS kb_search_zero_thin,
    CASE
        WHEN count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved') = 0
            THEN NULL
        ELSE (count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved'
                               AND coalesce(cardinality(returned_artifact_ids), 0) <= 1))::float
             / count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved')
    END AS kb_search_zero_thin_rate
FROM retrieval_event
GROUP BY 1
"""

# Same retrieval_event aggregate as V_RETRIEVAL_HEALTH_V1 (the `re` CTE below is
# byte-identical to it except for the trailing GROUP BY alias), LEFT JOINed to a
# per-day roll-up of kb_build_run's ledger-mining counters. Reads only
# retrieval_event and kb_build_run; never knowledge_artifact / body_text /
# query_text / normalized_query (statically asserted, test_dashboard_views.py).
V_RETRIEVAL_HEALTH = """
CREATE VIEW v_retrieval_health AS
WITH re AS (
    SELECT
        date_trunc('day', created_at)::date AS day,
        count(*) AS events,
        count(*) FILTER (WHERE status = 'approved') AS approved,
        count(*) FILTER (WHERE status = 'reused') AS reused,
        count(*) FILTER (WHERE status = 'denied') AS denied,
        count(*) FILTER (WHERE status = 'needs_human_approval') AS needs_human_approval,
        count(*) FILTER (WHERE status = 'error') AS errors,
        (count(*) FILTER (WHERE status = 'error'))::float / count(*) AS error_rate,
        CASE
            WHEN count(*) FILTER (WHERE status IN ('reused', 'approved')) = 0 THEN NULL
            ELSE (count(*) FILTER (WHERE status = 'reused'))::float
                 / count(*) FILTER (WHERE status IN ('reused', 'approved'))
        END AS evidence_reuse_rate,
        CASE
            WHEN count(*) FILTER (WHERE tool_name = 'context.request_more'
                                  AND status IN ('approved', 'reused')) = 0 THEN NULL
            ELSE (count(*) FILTER (WHERE tool_name = 'context.request_more'
                                   AND status IN ('approved', 'reused')
                                   AND semantic_reuse))::float
                 / count(*) FILTER (WHERE tool_name = 'context.request_more'
                                    AND status IN ('approved', 'reused'))
        END AS semantic_cache_hit_rate,
        (count(*) FILTER (WHERE cache_hit))::float / count(*) AS cache_hit_rate,
        count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved')
            AS kb_search_answered,
        count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved'
                         AND coalesce(cardinality(returned_artifact_ids), 0) <= 1)
            AS kb_search_zero_thin,
        CASE
            WHEN count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved') = 0
                THEN NULL
            ELSE (count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved'
                                   AND coalesce(cardinality(returned_artifact_ids), 0) <= 1))::float
                 / count(*) FILTER (WHERE tool_name = 'kb_search' AND status = 'approved')
        END AS kb_search_zero_thin_rate
    FROM retrieval_event
    GROUP BY 1
),
lm AS (
    SELECT
        date_trunc('day', started_at)::date AS day,
        sum(ledger_mining_misses_seen)::bigint AS ledger_mining_misses_seen,
        sum(ledger_mining_mined)::bigint AS ledger_mined,
        sum(ledger_mining_unresolved)::bigint AS ledger_unresolved
    FROM kb_build_run
    GROUP BY 1
)
SELECT
    re.day,
    re.events,
    re.approved,
    re.reused,
    re.denied,
    re.needs_human_approval,
    re.errors,
    re.error_rate,
    re.evidence_reuse_rate,
    re.semantic_cache_hit_rate,
    re.cache_hit_rate,
    re.kb_search_answered,
    re.kb_search_zero_thin,
    re.kb_search_zero_thin_rate,
    coalesce(lm.ledger_mined, 0) AS ledger_mined,
    coalesce(lm.ledger_unresolved, 0) AS ledger_unresolved,
    CASE
        WHEN coalesce(lm.ledger_mined, 0) + coalesce(lm.ledger_unresolved, 0) = 0 THEN NULL
        ELSE lm.ledger_mined::float / (lm.ledger_mined + lm.ledger_unresolved)
    END AS ledger_mined_rate
FROM re
LEFT JOIN lm ON lm.day = re.day
"""


def upgrade() -> None:
    op.execute("DROP VIEW v_retrieval_health")
    op.execute(V_RETRIEVAL_HEALTH)


def downgrade() -> None:
    op.execute("DROP VIEW v_retrieval_health")
    op.execute(V_RETRIEVAL_HEALTH_V1)
