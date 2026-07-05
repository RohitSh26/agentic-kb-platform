"""Dashboard views (migration 0020, ADR-0014 Phase 1) — round-trip + drift tests.

DB tests require an externally reachable TEST_DATABASE_URL (make migrate-test-db
convention). The budget-literal drift test and the aggregate-only static test run
without a database: the ALLOWED_EDGE_TYPES precedent (this file's sibling
test_publish_gates.py) showed that a doc updated without its enforcement — or the
reverse — ships a live failure, so the view's encoded budget numbers are pinned
to .claude/rules/token-budgets.md here.
"""

import asyncio
import importlib.util
import os
import re
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None, reason="no test database configured (set TEST_DATABASE_URL)"
)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0020_dashboard_views.py"
)
TOKEN_BUDGETS_RULE = Path(__file__).resolve().parents[4] / ".claude" / "rules" / "token-budgets.md"

VIEW_NAMES = ("v_retrieval_health", "v_token_economics", "v_build_health", "v_budget_adherence")


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0020_dashboard_views", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alembic_config() -> Config:
    assert TEST_DATABASE_URL is not None
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    return Config(str(ALEMBIC_INI))


async def _existing_views(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            present: set[str] = set()
            for name in VIEW_NAMES:
                found = await session.execute(text("SELECT to_regclass(:name)"), {"name": name})
                if found.scalar_one_or_none() is not None:
                    present.add(name)
            return present
    finally:
        await engine.dispose()


@requires_db
def test_0020_up_down_up_round_trip() -> None:
    """Upgrade creates all four views; downgrade drops all four; re-upgrade restores them."""
    assert TEST_DATABASE_URL is not None
    cfg = _alembic_config()
    command.upgrade(cfg, "0020")
    assert asyncio.run(_existing_views(TEST_DATABASE_URL)) == set(VIEW_NAMES)

    command.downgrade(cfg, "0019")
    assert asyncio.run(_existing_views(TEST_DATABASE_URL)) == set()

    command.upgrade(cfg, "0020")
    assert asyncio.run(_existing_views(TEST_DATABASE_URL)) == set(VIEW_NAMES)
    # Leave at head for subsequent tests in the same run.
    command.upgrade(cfg, "head")


@requires_db
def test_views_aggregate_seeded_ledger_rows() -> None:
    """Sanity over seeded rows: reuse rate, KB-gap proxy, and a per-run budget breach."""
    assert TEST_DATABASE_URL is not None
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    db_url = TEST_DATABASE_URL

    async def _check() -> None:
        engine = create_async_engine(db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await session.execute(
                    text("DELETE FROM retrieval_event WHERE kb_version = 'kb-dash-views'")
                )
                rows = (
                    # (run_id, tool_name, status, tokens, returned_ids_sql)
                    ("run-v1", "context.create_pack", "approved", 5000, "NULL"),
                    ("run-v1", "context.request_more", "reused", 500, "NULL"),
                    ("-", "kb_search", "approved", 0, "CAST(ARRAY[] AS uuid[])"),
                    ("run-v2", "context.create_pack", "approved", 20000, "NULL"),
                )
                for run_id, tool, status, tokens, returned in rows:
                    await session.execute(
                        text(
                            "INSERT INTO retrieval_event (run_id, agent_name, tool_name,"
                            " status, kb_version, tokens_returned, returned_artifact_ids)"
                            f" VALUES (:run_id, 'implementation', :tool, :status,"
                            f" 'kb-dash-views', :tokens, {returned})"
                        ),
                        {"run_id": run_id, "tool": tool, "status": status, "tokens": tokens},
                    )
                await session.commit()

                health = (
                    (
                        await session.execute(
                            text(
                                "SELECT events, reused, approved, evidence_reuse_rate,"
                                " kb_search_answered, kb_search_zero_thin_rate"
                                " FROM v_retrieval_health WHERE day = CURRENT_DATE"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert health["events"] >= 4
                assert health["kb_search_answered"] >= 1
                assert health["kb_search_zero_thin_rate"] == pytest.approx(1.0)

                breach = (
                    (
                        await session.execute(
                            text(
                                "SELECT run_tokens, run_budget_tokens, over_run_budget"
                                " FROM v_budget_adherence WHERE run_id = 'run-v2'"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert breach["run_tokens"] == 20000
                assert breach["over_run_budget"] is True

                within = (
                    (
                        await session.execute(
                            text(
                                "SELECT over_run_budget, follow_up_requests, over_agent_requests"
                                " FROM v_budget_adherence WHERE run_id = 'run-v1'"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert within["over_run_budget"] is False
                assert within["follow_up_requests"] == 1
                assert within["over_agent_requests"] is False

                # the no-run sentinel is not a run — kb_search rows never appear here
                sentinel = await session.execute(
                    text("SELECT count(*) FROM v_budget_adherence WHERE run_id = '-'")
                )
                assert sentinel.scalar_one() == 0

                await session.execute(
                    text("DELETE FROM retrieval_event WHERE kb_version = 'kb-dash-views'")
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_check())


# --------------------------------------------------------------------------
# Budget-literal drift (no DB): the view encodes .claude/rules/token-budgets.md
# numbers as literals. Parse the rules file and pin the migration's constants
# to it — the same protection pattern as the relation-ontology drift test.
# --------------------------------------------------------------------------

# rules-file role label -> the ledger agent_name (agents/ manifest name).
_LABEL_TO_AGENT = {
    "Implementation agent": "implementation",
    "Test agent": "test_layer",
    "Code reviewer": "code_reviewer",
    "Delivery planner": "delivery_planner",
    "PR planner": "pr_planner",
    "ADR writer": "adr_writer",
    "Infra code": "infra_code",
    "Bug reviewer": "bug_reviewer",
    "Security reviewer": "security_reviewer",
    "Quality reviewer": "quality_reviewer",
    "Test coverage reviewer": "test_coverage_reviewer",
}

# The rules file writes ranges with an EN DASH (U+2013); accept a plain hyphen
# too. A band may also be a single value ("2 requests / 3k tokens") — the view
# encodes the UPPER bound either way.
_RANGE_SEP = "[\u2013-]"
_RUN_BUDGET_LINE = re.compile(
    rf"Full run context budget[^:]*:\s*(?:[\d.]+k{_RANGE_SEP})?([\d.]+)k tokens"
)
_AGENT_LINE = re.compile(
    rf"^- (?P<label>.+?) extra:\s*(?P<requests>\d+) requests? / "
    rf"(?:[\d.]+k{_RANGE_SEP})?(?P<upper>[\d.]+)k tokens",
    re.MULTILINE,
)


def _tokens(upper_k: str) -> int:
    return int(float(upper_k) * 1000)


def _rules_run_budget() -> int:
    match = _RUN_BUDGET_LINE.search(TOKEN_BUDGETS_RULE.read_text(encoding="utf-8"))
    assert match is not None, (
        f"could not parse the full-run budget line from {TOKEN_BUDGETS_RULE} — "
        "fix the parser, do not hardcode the view around it"
    )
    return _tokens(match.group(1))


def _rules_agent_allowances() -> dict[str, tuple[int, int]]:
    allowances: dict[str, tuple[int, int]] = {}
    for match in _AGENT_LINE.finditer(TOKEN_BUDGETS_RULE.read_text(encoding="utf-8")):
        agent = _LABEL_TO_AGENT.get(match.group("label"))
        assert agent is not None, (
            f"token-budgets.md has a role line the dashboard does not know: "
            f"{match.group('label')!r} — add it to v_budget_adherence (new migration) "
            "and to this test's _LABEL_TO_AGENT map"
        )
        allowances[agent] = (int(match.group("requests")), _tokens(match.group("upper")))
    return allowances


def test_budget_view_literals_match_token_budgets_rule() -> None:
    migration = _load_migration()
    rules_allowances = _rules_agent_allowances()
    assert len(rules_allowances) >= 5, (
        f"parsed only {sorted(rules_allowances)} from {TOKEN_BUDGETS_RULE} — "
        "the rules-file parse silently broke; fix the parser, do not weaken the pin"
    )
    assert _rules_run_budget() == migration.RUN_BUDGET_TOKENS, (
        "v_budget_adherence's run-budget literal drifted from token-budgets.md — "
        "write a new migration replacing the view (never edit an applied one)"
    )
    assert rules_allowances == migration.AGENT_ALLOWANCES, (
        "v_budget_adherence's per-agent allowance literals drifted from token-budgets.md — "
        "write a new migration replacing the view (never edit an applied one)"
    )


def test_budget_literals_are_actually_in_the_view_sql() -> None:
    """The constants must be the values the SQL ships, not decoration beside it."""
    migration = _load_migration()
    sql = migration.V_BUDGET_ADHERENCE
    assert f"{migration.RUN_BUDGET_TOKENS} AS run_budget_tokens" in sql
    for agent, (max_requests, max_tokens) in migration.AGENT_ALLOWANCES.items():
        assert f"('{agent}', {max_requests}, {max_tokens})" in sql


def test_views_never_touch_content_columns() -> None:
    """Aggregate-only ACL posture (ADR-0014): id/type-level metadata only."""
    migration = _load_migration()
    forbidden = ("query_text", "normalized_query", "body_text", "knowledge_artifact")
    for sql in migration.VIEW_SQL:
        for column in forbidden:
            assert column not in sql, f"dashboard view SQL must never reference {column}"
