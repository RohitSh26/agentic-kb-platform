"""Dashboard renderer (ADR-0014 Phase 1) — seeded-ledger rates + aggregate-only posture.

The DB tests require a migrated TEST_DATABASE_URL (make migrate-test-db) and seed
a full status matrix — approved / reused / denied / error, kb_search zero- and
thin-result rows, an over-budget run, and an over-allowance agent — then assert
the exact rates the views compute and the report renders. The static tests pin
the renderer to the aggregate-only ACL posture (never query_text / body_text)
and to the harness's pinned metric names.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from harness.dashboard import (
    ALL_SQL,
    LEDGER_METRIC_NAMES,
    DashboardData,
    fetch_dashboard,
    generate_dashboard,
    render_html,
    render_markdown,
)
from harness.fixtures import RegistryNotMigratedError, clean_registry, require_registry_schema
from harness.metrics import METRIC_NAMES

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="TEST_DATABASE_URL not set (needs a migrated registry: make migrate-test-db)",
)

KB_VERSION = "kb-dash"


# --- static: the aggregate-only posture and the pinned names (no DB) ---------------


def test_renderer_sql_never_selects_content_columns() -> None:
    """ADR-0014 privacy posture: id/type-level metadata only, never content."""
    for sql in ALL_SQL:
        for forbidden in ("query_text", "normalized_query", "body_text", "knowledge_artifact"):
            assert forbidden not in sql, f"renderer SQL must never reference {forbidden}"
        assert "SELECT *" not in sql, "explicit column lists only — SELECT * hides a leak"


def test_shared_dashboard_columns_are_pinned_metric_names() -> None:
    """A gate and its dashboard tile can never disagree on a definition (ADR-0014)."""
    assert set(LEDGER_METRIC_NAMES) <= set(METRIC_NAMES)
    # and the renderer actually selects each pinned name from the views
    joined = "\n".join(ALL_SQL)
    for name in LEDGER_METRIC_NAMES:
        assert name in joined, f"pinned metric {name} is declared but never selected"


# --- seeded-ledger fixture ----------------------------------------------------------


async def _seed(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        try:
            await require_registry_schema(session)
        except RegistryNotMigratedError as error:
            pytest.skip(str(error))
        views = await session.execute(text("SELECT to_regclass('v_retrieval_health')"))
        if views.scalar_one_or_none() is None:
            raise RegistryNotMigratedError(
                "dashboard views missing — run kb-builder migrations (make migrate-test-db)"
            )
    async with factory() as session:
        await clean_registry(session)
    thin_id = uuid.uuid4()
    healthy_ids = [uuid.uuid4() for _ in range(3)]
    events = (
        # (run_id, agent, tool, status, semantic_reuse, tokens, returned uuid[] literal)
        ("run-a", "implementation", "context.create_pack", "approved", False, 5000, None),
        ("run-a", "implementation", "context.request_more", "approved", False, 1000, None),
        ("run-a", "implementation", "context.request_more", "reused", True, 500, None),
        ("run-a", "implementation", "context.request_more", "denied", False, 0, None),
        ("run-b", "test_layer", "context.create_pack", "error", False, 0, None),
        ("-", "implementation", "kb_search", "approved", False, 0, "ARRAY[]::uuid[]"),
        ("-", "implementation", "kb_search", "approved", False, 100, f"ARRAY['{thin_id}']::uuid[]"),
        (
            "-",
            "implementation",
            "kb_search",
            "approved",
            False,
            300,
            "ARRAY[" + ",".join(f"'{i}'" for i in healthy_ids) + "]::uuid[]",
        ),
        ("-", "implementation", "kb_search", "denied", False, 0, None),
        # a whole-run budget breach: 20000 > the 18000 full-run cap
        ("run-big", "implementation", "context.create_pack", "approved", False, 20000, None),
        # an agent-allowance breach: pr_planner is allowed 1 follow-up request
        ("run-c", "pr_planner", "context.request_more", "approved", False, 200, None),
        ("run-c", "pr_planner", "context.request_more", "approved", False, 200, None),
    )
    async with factory() as session:
        for run_id, agent, tool, status, semantic, tokens, returned in events:
            returned_sql = returned if returned is not None else "NULL"
            await session.execute(
                text(
                    "INSERT INTO retrieval_event (run_id, agent_name, tool_name, status,"
                    " semantic_reuse, tokens_returned, kb_version, returned_artifact_ids)"
                    f" VALUES (:run_id, :agent, :tool, :status, :semantic, :tokens,"
                    f" :kb_version, {returned_sql})"
                ),
                {
                    "run_id": run_id,
                    "agent": agent,
                    "tool": tool,
                    "status": status,
                    "semantic": semantic,
                    "tokens": tokens,
                    "kb_version": KB_VERSION,
                },
            )
        # builds: an active one (published an hour ago) and a newer failed publish
        await session.execute(
            text(
                "INSERT INTO kb_build_run (kb_version, build_seq, status, completed_at,"
                " sources_seen, sources_changed, llm_calls, embedding_calls)"
                " VALUES ('kb-dash-active', 9101, 'active', now() - interval '1 hour',"
                " 10, 5, 10, 10)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO kb_build_run (kb_version, build_seq, status, completed_at,"
                " sources_seen, sources_changed, llm_calls, failed_gate, gate_measured_value)"
                " VALUES ('kb-dash-failed', 9102, 'validation_failed', now(),"
                " 10, 5, 10, 'edge_evidence_integrity', 1.0)"
            )
        )
        await session.commit()


@pytest.fixture
async def seeded_data() -> AsyncIterator[DashboardData]:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    try:
        await _seed(engine)
        data = await fetch_dashboard(DATABASE_URL, report_path=None)
        yield data
    finally:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await clean_registry(session)
        await engine.dispose()


# --- computed rates over the seeded matrix -----------------------------------------


@requires_db
async def test_retrieval_health_rates(seeded_data: DashboardData) -> None:
    today = seeded_data.retrieval_health[0]
    assert today["events"] == 12
    assert today["approved"] == 8
    assert today["reused"] == 1
    assert today["denied"] == 2
    assert today["errors"] == 1
    assert today["error_rate"] == pytest.approx(1 / 12)
    assert today["evidence_reuse_rate"] == pytest.approx(1 / 9)
    # charged follow-ups: 3 approved + 1 reused request_more; 1 semantic
    assert today["semantic_cache_hit_rate"] == pytest.approx(1 / 4)
    # KB-gap proxy: 3 answered kb_search, one empty + one single-hit = zero/thin
    assert today["kb_search_answered"] == 3
    assert today["kb_search_zero_thin"] == 2
    assert today["kb_search_zero_thin_rate"] == pytest.approx(2 / 3)


@requires_db
async def test_token_economics_rates(seeded_data: DashboardData) -> None:
    today = seeded_data.token_economics[0]
    assert today["tokens_charged"] == 27300
    # run-a, run-b, run-big, run-c — the '-' kb_search sentinel is not a run
    assert today["runs"] == 4
    assert today["context_tokens_per_run"] == pytest.approx(26900 / 4)
    assert today["agents"] == 3
    assert today["retrieval_calls_per_agent"] == pytest.approx(4.0)


@requires_db
async def test_budget_breaches_flag_only_the_offenders(seeded_data: DashboardData) -> None:
    breaches = {(row["run_id"], row["agent_name"]): row for row in seeded_data.budget_breaches}
    assert set(breaches) == {("run-big", "implementation"), ("run-c", "pr_planner")}

    over_run = breaches[("run-big", "implementation")]
    assert over_run["over_run_budget"] is True
    assert over_run["run_tokens"] == 20000
    assert over_run["run_budget_tokens"] == 18000

    over_agent = breaches[("run-c", "pr_planner")]
    assert over_agent["over_run_budget"] is False
    assert over_agent["over_agent_requests"] is True
    assert over_agent["follow_up_requests"] == 2
    assert over_agent["agent_max_requests"] == 1
    assert over_agent["over_agent_tokens"] is False

    # the in-budget run and the kb_search sentinel never appear as breaches
    assert seeded_data.budget_totals["runs_over_budget"] == 1
    assert seeded_data.budget_totals["agents_over_allowance"] == 1


@requires_db
async def test_build_health_rows(seeded_data: DashboardData) -> None:
    latest = seeded_data.build_health[0]
    assert latest["kb_version"] == "kb-dash-failed"
    assert latest["status"] == "validation_failed"
    assert latest["failed_gate"] == "edge_evidence_integrity"
    assert latest["llm_calls_per_changed_source"] == pytest.approx(2.0)

    active = next(row for row in seeded_data.build_health if row["is_active"])
    assert active["kb_version"] == "kb-dash-active"
    assert 3500 <= float(active["active_kb_age_seconds"]) <= 3900


# --- rendered artifacts -------------------------------------------------------------


@requires_db
async def test_markdown_and_html_show_the_operator_answers(seeded_data: DashboardData) -> None:
    markdown = render_markdown(seeded_data)
    assert "validation_failed (gate: edge_evidence_integrity)" in markdown
    assert "66.7%" in markdown  # KB-gap proxy tile
    assert "27,300" in markdown  # tokens this week
    assert "Budget breaches (runs over / agents over): **1 / 1**" in markdown
    assert "run-big" in markdown and "run-c" in markdown

    html = render_html(seeded_data)
    assert html.startswith("<!doctype html>")
    assert "run-big" in html
    assert 'class="tile bad"' in html  # the failed build / breach tiles are colored
    # aggregate-only: no seeded content columns can appear because none is selected
    assert "query_text" not in html and "body_text" not in html


@requires_db
async def test_generate_dashboard_writes_both_files(tmp_path: Path) -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    try:
        await _seed(engine)
        report_path = tmp_path / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "created_at": "2026-07-05T00:00:00+00:00",
                    "git_sha": "abc1234",
                    "golden": {
                        "cases": 3,
                        "mean_evidence_recall": 1.0,
                        "min_evidence_recall": 1.0,
                        "total_acl_leaks": 0,
                        "cases_below_floor": [],
                        "intent_ordering_failures": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        html_path, md_path = await generate_dashboard(
            DATABASE_URL, tmp_path / "out", report_path=report_path
        )
        assert html_path.exists() and md_path.exists()
        markdown = md_path.read_text(encoding="utf-8")
        assert "Golden gate (floor 0.95, latest eval run)" in markdown
        assert "acl_leaks 0" in markdown
    finally:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await clean_registry(session)
        await engine.dispose()
