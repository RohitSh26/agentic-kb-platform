"""kb_build_run ledger-mining counters (migration 0022) + v_retrieval_health
mined-vs-unresolved split (migration 0023) — round-trip + seeded-data +
aggregate-only static checks. Same conventions as
`test_dashboard_views.py` (migration 0020).

DB tests require an externally reachable TEST_DATABASE_URL (make migrate-test-db
convention). The aggregate-only static test runs without a database.
"""

import asyncio
import importlib.util
import os
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

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
MIGRATION_0022 = MIGRATIONS_DIR / "0022_kb_build_run_ledger_mining_counters.py"
MIGRATION_0023 = MIGRATIONS_DIR / "0023_v_retrieval_health_ledger_mining_split.py"

_NEW_COLUMNS = ("ledger_mining_misses_seen", "ledger_mining_mined", "ledger_mining_unresolved")


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alembic_config() -> Config:
    assert TEST_DATABASE_URL is not None
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    return Config(str(ALEMBIC_INI))


async def _kb_build_run_columns(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'kb_build_run'"
                )
            )
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


async def _view_exists(database_url: str, name: str) -> bool:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            found = await session.execute(text("SELECT to_regclass(:name)"), {"name": name})
            return found.scalar_one_or_none() is not None
    finally:
        await engine.dispose()


@requires_db
def test_0022_up_down_up_round_trip() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0022")
    assert TEST_DATABASE_URL is not None
    columns = asyncio.run(_kb_build_run_columns(TEST_DATABASE_URL))
    assert set(_NEW_COLUMNS) <= columns

    command.downgrade(cfg, "0021")
    columns = asyncio.run(_kb_build_run_columns(TEST_DATABASE_URL))
    assert not (set(_NEW_COLUMNS) & columns)

    command.upgrade(cfg, "0022")
    columns = asyncio.run(_kb_build_run_columns(TEST_DATABASE_URL))
    assert set(_NEW_COLUMNS) <= columns
    command.upgrade(cfg, "head")


@requires_db
def test_0023_up_down_up_round_trip() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    assert TEST_DATABASE_URL is not None
    assert asyncio.run(_view_exists(TEST_DATABASE_URL, "v_retrieval_health"))

    command.downgrade(cfg, "0022")
    assert asyncio.run(_view_exists(TEST_DATABASE_URL, "v_retrieval_health"))  # 0020's def stands

    command.upgrade(cfg, "0023")
    assert asyncio.run(_view_exists(TEST_DATABASE_URL, "v_retrieval_health"))
    command.upgrade(cfg, "head")


@requires_db
def test_v_retrieval_health_rolls_up_kb_build_run_ledger_mining_counters() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    db_url = TEST_DATABASE_URL
    assert db_url is not None

    async def _check() -> None:
        engine = create_async_engine(db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await session.execute(
                    text("DELETE FROM retrieval_event WHERE kb_version = 'kb-ledger-dash-views'")
                )
                await session.execute(
                    text("DELETE FROM kb_build_run WHERE kb_version LIKE 'kb-ledger-dash-views%'")
                )
                await session.execute(
                    text(
                        "INSERT INTO retrieval_event (run_id, agent_name, tool_name, status,"
                        " kb_version, tokens_returned, returned_artifact_ids)"
                        " VALUES ('-', 'implementation', 'kb_search', 'approved',"
                        " 'kb-ledger-dash-views', 0, CAST(ARRAY[] AS uuid[]))"
                    )
                )
                await session.execute(
                    text(
                        "INSERT INTO kb_build_run (kb_version, build_seq, status,"
                        " ledger_mining_misses_seen, ledger_mining_mined,"
                        " ledger_mining_unresolved)"
                        " VALUES ('kb-ledger-dash-views-1', 9201, 'completed', 5, 3, 2)"
                    )
                )
                # a SECOND build the SAME day sums into the same day-row
                await session.execute(
                    text(
                        "INSERT INTO kb_build_run (kb_version, build_seq, status,"
                        " ledger_mining_misses_seen, ledger_mining_mined,"
                        " ledger_mining_unresolved)"
                        " VALUES ('kb-ledger-dash-views-2', 9202, 'completed', 1, 1, 0)"
                    )
                )
                await session.commit()

                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT ledger_mined, ledger_unresolved, ledger_mined_rate"
                                " FROM v_retrieval_health WHERE day = CURRENT_DATE"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["ledger_mined"] == 4
                assert row["ledger_unresolved"] == 2
                assert row["ledger_mined_rate"] == pytest.approx(4 / 6)

                await session.execute(
                    text("DELETE FROM retrieval_event WHERE kb_version = 'kb-ledger-dash-views'")
                )
                await session.execute(
                    text("DELETE FROM kb_build_run WHERE kb_version LIKE 'kb-ledger-dash-views%'")
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_check())


def test_v_retrieval_health_never_touches_content_columns() -> None:
    """Aggregate-only ACL posture (ADR-0014): never query_text / normalized_query /
    body_text / knowledge_artifact, even after the ledger-mining split join."""
    migration = _load_module(MIGRATION_0023, "migration_0023_ledger_mining_split")
    forbidden = ("query_text", "normalized_query", "body_text", "knowledge_artifact")
    for sql in (migration.V_RETRIEVAL_HEALTH, migration.V_RETRIEVAL_HEALTH_V1):
        for column in forbidden:
            assert column not in sql, f"dashboard view SQL must never reference {column}"
