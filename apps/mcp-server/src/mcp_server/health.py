"""Readiness payload: the active kb_version straight from the registry.

MCP serves the last successful active kb_version (invariant 5); the partial
unique index on kb_build_run guarantees at most one 'active' row, so this is
a scalar lookup. No active version yet (fresh registry) means not ready —
the route maps that to 503, not an error.
"""

from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.logging import get_logger
from db.models import KbBuildRun

logger = get_logger("mcp_server.health")


class HealthPayload(TypedDict):
    status: str
    service: str
    active_kb_version: str | None


async def health(session_factory: async_sessionmaker[AsyncSession]) -> HealthPayload:
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(KbBuildRun.kb_version).where(KbBuildRun.status == "active")
            )
            active = result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        logger.error("event=health_registry_unreachable error=%s", type(exc).__name__)
        return {
            "status": "registry_unreachable",
            "service": "mcp-server",
            "active_kb_version": None,
        }
    return {
        "status": "ok" if active is not None else "no_active_kb_version",
        "service": "mcp-server",
        "active_kb_version": active,
    }
