"""Single-builder advisory lock (task #33 — the 2026-07-05 zombie incident).

Two builders racing on one registry is an OPERATIONAL FAULT, not a race worth
resolving after the fact: the concurrent-writer fence in `invalidation.py` and
`BuildRunner._assert_run_row_alive` neutralize the *damage* two interleaved
builders can do, but neither *prevents* the race from starting. A Postgres
session-level advisory lock, taken before a build does anything, does: at
most one process holds `BUILDER_LOCK_KEY` at a time, so a second builder never
begins racing in the first place.

`pg_try_advisory_lock` (never the blocking `pg_advisory_lock`): a second
builder aborts immediately and loudly (`event=builder_lock_held`) instead of
queuing silently — an operator watching the log sees the fault right away
instead of a build that appears to hang.

Session-level, not `pg_advisory_xact_lock`: the lock must outlive every
individual transaction the build commits per-source (docs/architecture — the
runner commits as each source's knowledge is ready), so it is held on ONE
DEDICATED connection checked out for the whole build and released explicitly
on exit — never tied to the ORM build session's own connection/transaction
lifecycle, which churns throughout the run.
"""

import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_LOCK_NAMESPACE = "agentic_kb_builder"
# A stable, deterministic key derived from a fixed namespace string — never Python's
# own hash() (process-randomized by PYTHONHASHSEED, so it would differ builder to
# builder and defeat the lock). crc32 always returns an unsigned 32-bit value, well
# within pg_try_advisory_lock(bigint)'s single-key range on any Postgres version.
BUILDER_LOCK_KEY: Final[int] = zlib.crc32(_LOCK_NAMESPACE.encode("utf-8"))


class BuilderLockUnavailableError(RuntimeError):
    """Another process already holds the single-builder advisory lock."""


@asynccontextmanager
async def acquire_builder_lock(
    engine: AsyncEngine, *, key: int = BUILDER_LOCK_KEY
) -> AsyncIterator[None]:
    """Hold the single-builder advisory lock for the `async with` block's duration.

    Opens its OWN connection from `engine` — never the build's ORM session, whose
    connection/transaction churns across the run (a per-source commit), so a lock
    tied to it could not be guaranteed to outlive the whole build. Raises
    `BuilderLockUnavailableError` immediately (never blocks, never queues) when
    another process already holds it; the caller decides how to surface that
    (the CLI aborts the process). Released in a `finally` so a crash inside the
    `async with` block still releases it before the connection closes.
    """
    conn = await engine.connect()
    try:
        acquired = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        ).scalar_one()
        await conn.commit()
        if not acquired:
            logger.error(
                "event=builder_lock_held key=%d reason=another_builder_is_running "
                "action=abort_immediately",
                key,
            )
            raise BuilderLockUnavailableError(
                f"another build already holds the single-builder advisory lock "
                f"(key={key}); refusing to start a second concurrent build"
            )
        logger.info("event=builder_lock_acquired key=%d", key)
        try:
            yield
        finally:
            released = (
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            ).scalar_one()
            await conn.commit()
            logger.info("event=builder_lock_released key=%d released=%s", key, released)
    finally:
        await conn.close()


__all__ = ["BUILDER_LOCK_KEY", "BuilderLockUnavailableError", "acquire_builder_lock"]
