"""Dashboard views — ADR-0014 Phase 1 (docs/contracts/observability-dashboard.md).

Four read-only, aggregate-only VIEWS over retrieval_event and kb_build_run:
v_retrieval_health, v_token_economics, v_build_health, v_budget_adherence. Pure
projections — no tables, no columns, no data; the downgrade drops them and loses
nothing (invariant 1). They read ledger METADATA only (statuses, counts, token
totals, id-array cardinality) — never query_text / normalized_query / body_text
(the aggregate-only ACL posture ratified in ADR-0014).

v_budget_adherence encodes the current numbers from .claude/rules/token-budgets.md
as literals (a view cannot read a rules file). The constants below are the single
in-migration source for those literals; tests/integration/test_dashboard_views.py
parses the rules file and fails on drift (the ALLOWED_EDGE_TYPES precedent).

Verified up->down->up.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-05

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- Budget literals (source: .claude/rules/token-budgets.md — drift-tested) ------------------
# Upper bound of the "Full run context budget: 12k-18k tokens" band.
RUN_BUDGET_TOKENS = 18_000
# agent_name -> (max extra requests, max extra tokens): the upper bound of each
# role's "extra" line in the rules file. Keys match agents/ manifest names (the
# ledger's agent_name is the authenticated subject).
AGENT_ALLOWANCES: dict[str, tuple[int, int]] = {
    "implementation": (2, 4_000),
    "test_layer": (1, 2_500),
    "code_reviewer": (1, 2_500),
    "delivery_planner": (1, 1_500),
    "pr_planner": (1, 1_500),
    "adr_writer": (2, 3_000),
    "infra_code": (2, 3_000),
    "bug_reviewer": (1, 2_000),
    "security_reviewer": (1, 2_000),
    "quality_reviewer": (1, 2_000),
    "test_coverage_reviewer": (1, 2_000),
}
# Fallback for agents absent from the map — mirrors the broker's
# DEFAULT_AGENT_ALLOWANCE (mcp-server context_broker/budgets.py: 1 request / 2500).
DEFAULT_AGENT_MAX_REQUESTS = 1
DEFAULT_AGENT_MAX_TOKENS = 2_500

# The broker's no-run sentinel (mcp-server context_broker/constants.py): kb_search
# and unresolved-error rows carry run_id='-'. Not a run — excluded from run-grain
# budget adherence (kb_search budgets are enforced per session window server-side).
NO_RUN_SENTINEL = "-"

V_RETRIEVAL_HEALTH = """
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

# runs / context_tokens_per_run exclude the '-' no-run sentinel (kb_search and
# session-scoped tools): the sentinel is not a run and would skew per-run tokens.
# tokens_charged / events / agents stay corpus-wide.
V_TOKEN_ECONOMICS = f"""
CREATE VIEW v_token_economics AS
SELECT
    date_trunc('day', created_at)::date AS day,
    count(DISTINCT run_id) FILTER (WHERE run_id <> '{NO_RUN_SENTINEL}') AS runs,
    count(DISTINCT agent_name) AS agents,
    count(*) AS events,
    sum(coalesce(tokens_returned, 0))::bigint AS tokens_charged,
    CASE
        WHEN count(DISTINCT run_id) FILTER (WHERE run_id <> '{NO_RUN_SENTINEL}') = 0 THEN NULL
        ELSE coalesce(sum(coalesce(tokens_returned, 0))
                      FILTER (WHERE run_id <> '{NO_RUN_SENTINEL}'), 0)::float
             / count(DISTINCT run_id) FILTER (WHERE run_id <> '{NO_RUN_SENTINEL}')
    END AS context_tokens_per_run,
    count(*)::float / count(DISTINCT agent_name) AS retrieval_calls_per_agent
FROM retrieval_event
GROUP BY 1
"""

V_BUILD_HEALTH = """
CREATE VIEW v_build_health AS
SELECT
    build_id,
    kb_version,
    build_seq,
    status,
    started_at,
    completed_at,
    extract(epoch FROM (completed_at - started_at)) AS duration_seconds,
    sources_seen,
    sources_changed,
    artifacts_created,
    artifacts_updated,
    artifacts_deleted,
    llm_calls,
    embedding_calls,
    CASE WHEN sources_changed = 0 THEN NULL
         ELSE llm_calls::float / sources_changed
    END AS llm_calls_per_changed_source,
    CASE WHEN sources_changed = 0 THEN NULL
         ELSE embedding_calls::float / sources_changed
    END AS embedding_calls_per_changed_source,
    extractor_failures,
    failed_gate,
    gate_measured_value,
    error_summary,
    status = 'active' AS is_active,
    CASE WHEN status = 'active'
         THEN extract(epoch FROM (now() - coalesce(completed_at, started_at)))
    END AS active_kb_age_seconds
FROM kb_build_run
"""


def _allowance_values() -> str:
    return ",\n        ".join(
        f"('{agent}', {max_requests}, {max_tokens})"
        for agent, (max_requests, max_tokens) in AGENT_ALLOWANCES.items()
    )


V_BUDGET_ADHERENCE = f"""
CREATE VIEW v_budget_adherence AS
WITH allowance(agent_name, max_requests, max_tokens) AS (
    VALUES
        {_allowance_values()}
),
per_agent AS (
    SELECT
        run_id,
        agent_name,
        count(*) AS events,
        sum(coalesce(tokens_returned, 0))::bigint AS tokens_charged,
        count(*) FILTER (WHERE tool_name = 'context.request_more'
                         AND status IN ('approved', 'reused')) AS follow_up_requests,
        coalesce(sum(tokens_returned) FILTER (WHERE tool_name = 'context.request_more'
                                              AND status IN ('approved', 'reused')), 0)::bigint
            AS follow_up_tokens
    FROM retrieval_event
    WHERE run_id <> '{NO_RUN_SENTINEL}'
    GROUP BY run_id, agent_name
),
per_run AS (
    SELECT run_id, sum(tokens_charged)::bigint AS run_tokens
    FROM per_agent
    GROUP BY run_id
)
SELECT
    pa.run_id,
    pa.agent_name,
    pa.events,
    pa.tokens_charged,
    pr.run_tokens,
    {RUN_BUDGET_TOKENS} AS run_budget_tokens,
    pr.run_tokens > {RUN_BUDGET_TOKENS} AS over_run_budget,
    pa.follow_up_requests,
    pa.follow_up_tokens,
    coalesce(a.max_requests, {DEFAULT_AGENT_MAX_REQUESTS}) AS agent_max_requests,
    coalesce(a.max_tokens, {DEFAULT_AGENT_MAX_TOKENS}) AS agent_max_tokens,
    pa.follow_up_requests > coalesce(a.max_requests, {DEFAULT_AGENT_MAX_REQUESTS})
        AS over_agent_requests,
    pa.follow_up_tokens > coalesce(a.max_tokens, {DEFAULT_AGENT_MAX_TOKENS})
        AS over_agent_tokens
FROM per_agent pa
JOIN per_run pr USING (run_id)
LEFT JOIN allowance a ON a.agent_name = pa.agent_name
"""

VIEW_NAMES = ("v_retrieval_health", "v_token_economics", "v_build_health", "v_budget_adherence")

VIEW_SQL = (V_RETRIEVAL_HEALTH, V_TOKEN_ECONOMICS, V_BUILD_HEALTH, V_BUDGET_ADHERENCE)


def upgrade() -> None:
    for sql in VIEW_SQL:
        op.execute(sql)


def downgrade() -> None:
    for name in reversed(VIEW_NAMES):
        op.execute(f"DROP VIEW IF EXISTS {name}")
