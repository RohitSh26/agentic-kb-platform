"""Single-builder advisory lock (task #33 — the 2026-07-05 zombie incident).

No schema is touched here: `pg_try_advisory_lock`/`pg_advisory_unlock` are Postgres
built-ins that need no migration, so these tests skip the `migrated_db`/Alembic
fixture other integration tests use and just open connections directly.

Skips when TEST_DATABASE_URL is unset (same shared-DB policy as the other
integration tests).
"""

import os
import zlib

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from agentic_kb_builder.application.builder_lock import (
    BuilderLockUnavailableError,
    acquire_builder_lock,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)


def _key(name: str) -> int:
    """A test-local lock key so tests never collide with each other or a real
    build sharing this Postgres instance."""
    return zlib.crc32(f"test-builder-lock-{name}".encode())


@requires_db
async def test_second_builder_aborts_while_first_holds_the_lock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two engines, real Postgres: a second builder's acquire fails immediately
    (never blocks/queues) while the first still holds the lock, and it says so
    loudly (`event=builder_lock_held`)."""
    assert TEST_DATABASE_URL is not None
    key = _key("second-builder-aborts")
    engine1 = create_async_engine(TEST_DATABASE_URL)
    engine2 = create_async_engine(TEST_DATABASE_URL)
    try:
        async with acquire_builder_lock(engine1, key=key):
            with caplog.at_level("ERROR"), pytest.raises(BuilderLockUnavailableError):
                async with acquire_builder_lock(engine2, key=key):
                    pytest.fail("the second builder must never enter the locked block")
            assert any(
                "event=builder_lock_held" in record.getMessage() for record in caplog.records
            )
    finally:
        await engine1.dispose()
        await engine2.dispose()


@requires_db
async def test_lock_is_released_after_a_completed_build(caplog: pytest.LogCaptureFixture) -> None:
    """Context-manager discipline: a normal exit releases the lock, so the next
    build (a fresh acquire) succeeds — the lock never leaks across builds."""
    assert TEST_DATABASE_URL is not None
    key = _key("released-after-completed")
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        with caplog.at_level("INFO"):
            async with acquire_builder_lock(engine, key=key):
                pass  # a "completed" build: the block exits normally
            async with acquire_builder_lock(engine, key=key):
                pass  # must succeed: the first hold already released it
        messages = [record.getMessage() for record in caplog.records]
        assert sum("event=builder_lock_acquired" in m for m in messages) == 2
        assert sum("event=builder_lock_released" in m for m in messages) == 2
    finally:
        await engine.dispose()


@requires_db
async def test_lock_is_released_after_a_crashed_build() -> None:
    """Context-manager discipline: an exception inside the block still releases
    the lock (the `finally`-guarded release), so a crashed build never leaves
    the registry permanently locked out for the next attempt."""
    assert TEST_DATABASE_URL is not None
    key = _key("released-after-crash")
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        with pytest.raises(RuntimeError, match="simulated build crash"):
            async with acquire_builder_lock(engine, key=key):
                raise RuntimeError("simulated build crash")
        async with acquire_builder_lock(engine, key=key):
            pass  # must succeed: the crash still released it
    finally:
        await engine.dispose()
