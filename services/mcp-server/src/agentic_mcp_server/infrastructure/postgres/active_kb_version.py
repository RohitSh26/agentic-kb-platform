"""Active kb_version lookup against the kb-builder-owned registry.

No ORM model import across the service boundary: the query names the table
and columns pinned in docs/contracts/postgres-knowledge-registry.md, and the
contract test asserts these constants stay in sync with that document. The
partial unique index on kb_build_run guarantees at most one 'active' row.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KB_BUILD_RUN_TABLE = "kb_build_run"
KB_VERSION_COLUMN = "kb_version"
STATUS_COLUMN = "status"

_ACTIVE_KB_VERSION_QUERY = text(
    f"SELECT {KB_VERSION_COLUMN} FROM {KB_BUILD_RUN_TABLE} WHERE {STATUS_COLUMN} = 'active'"
)


async def fetch_active_kb_version(session: AsyncSession) -> str | None:
    result = await session.execute(_ACTIVE_KB_VERSION_QUERY)
    return result.scalar_one_or_none()
