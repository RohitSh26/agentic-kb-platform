"""Migration 0017: retrieval_event.details JSONB — up/down round-trip tests.

Requires an externally migrated test database at TEST_DATABASE_URL (or DATABASE_URL).
Run `make migrate-test-db` in kb-builder first if the schema is behind.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _alembic_config() -> Config:
    assert TEST_DATABASE_URL is not None
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    return Config(str(ALEMBIC_INI))


def test_0017_up_down_round_trip() -> None:
    """0017 upgrade adds details; downgrade drops it; re-upgrade adds it again."""
    cfg = _alembic_config()
    command.upgrade(cfg, "0017")
    command.downgrade(cfg, "0016")
    command.upgrade(cfg, "0017")
    # Leave at head for subsequent tests in the same run.


def test_details_column_exists_after_upgrade() -> None:
    """After upgrade to head, the details column exists in retrieval_event."""
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    import asyncio

    assert TEST_DATABASE_URL is not None

    async def _check() -> None:
        engine = create_async_engine(TEST_DATABASE_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as session:
            # Insert a row with details = NULL (nullable check).
            await session.execute(
                text(
                    "INSERT INTO retrieval_event"
                    " (run_id, agent_name, tool_name, kb_version)"
                    " VALUES ('run-det-null', 'agent-x', 'context.create_pack', 'v1')"
                )
            )
            # Insert a row with a JSONB details payload.
            await session.execute(
                text(
                    "INSERT INTO retrieval_event"
                    " (run_id, agent_name, tool_name, kb_version, details)"
                    " VALUES ('run-det-json', 'agent-x', 'context.create_pack', 'v1',"
                    "  CAST(:details AS jsonb))"
                ),
                {
                    "details": (
                        '{"task": "test", "cards": [], "budget": {"allowed": 8000, "used": 100}}'
                    )
                },
            )
            await session.commit()

            null_row = await session.execute(
                text("SELECT details FROM retrieval_event WHERE run_id = 'run-det-null'")
            )
            assert null_row.scalar_one_or_none() is None

            json_row = await session.execute(
                text("SELECT details FROM retrieval_event WHERE run_id = 'run-det-json'")
            )
            details = json_row.scalar_one()
            assert details is not None
            assert details["task"] == "test"
            assert details["budget"]["allowed"] == 8000

            # Cleanup.
            await session.execute(
                text("DELETE FROM retrieval_event WHERE run_id IN ('run-det-null', 'run-det-json')")
            )
            await session.commit()

        await engine.dispose()

    asyncio.run(_check())
