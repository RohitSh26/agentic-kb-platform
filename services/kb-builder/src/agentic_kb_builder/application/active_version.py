"""Active kb_version handling (architecture invariant 5).

A kb_version goes active only after the validation hook passes. Activation
supersedes the previous active run and promotes the new one in a single
transaction; a failed validation never touches the previously active version,
so MCP keeps serving the last successful one. The single-active guarantee is
enforced in Postgres by the partial unique index uq_kb_build_run_single_active.
"""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.infrastructure.postgres.models import KbBuildRun
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

ValidationHook = Callable[[AsyncSession, str], Awaitable[bool]]
"""(session, kb_version) -> True when retrieval/index consistency validation passes."""


async def get_active_kb_version(session: AsyncSession) -> str | None:
    return (
        await session.execute(select(KbBuildRun.kb_version).where(KbBuildRun.status == "active"))
    ).scalar_one_or_none()


async def activate_kb_version(
    session: AsyncSession, build_id: uuid.UUID, validate: ValidationHook
) -> bool:
    """Promote the build's kb_version to active iff validation passes.

    Returns True on activation. On validation failure the run is marked
    validation_failed and the previously active version remains active.
    """
    run = await session.get(KbBuildRun, build_id)
    if run is None:
        raise ValueError(f"unknown build_id {build_id}")
    if run.status != "completed":
        raise ValueError(f"build {build_id} has status {run.status!r}, expected 'completed'")

    if not await validate(session, run.kb_version):
        run.status = "validation_failed"
        await session.flush()
        logger.error(
            "event=kb_version_validation_failed build_id=%s kb_version=%s "
            "active_version_unchanged=true",
            build_id,
            run.kb_version,
        )
        return False

    await session.execute(
        update(KbBuildRun)
        .where(KbBuildRun.status == "active")
        .values(
            status="superseded", completed_at=func.coalesce(KbBuildRun.completed_at, func.now())
        )
    )
    run.status = "active"
    await session.flush()
    logger.info("event=kb_version_activated build_id=%s kb_version=%s", build_id, run.kb_version)
    return True
