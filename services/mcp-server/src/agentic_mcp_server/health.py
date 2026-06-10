"""Readiness payload: the active kb_version straight from the registry.

MCP serves the last successful active kb_version (invariant 5); the partial
unique index on kb_build_run guarantees at most one 'active' row, so this is
a scalar lookup. No active version yet (fresh registry) means not ready —
the route maps that to 503, not an error.
"""

from typing import TypedDict

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_kb_version
from agentic_mcp_server.structured_logging import get_logger

logger = get_logger(__name__)


class HealthPayload(TypedDict):
    status: str
    service: str
    active_kb_version: str | None


async def health(session_factory: async_sessionmaker[AsyncSession]) -> HealthPayload:
    try:
        async with session_factory() as session:
            active = await fetch_active_kb_version(session)
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
