"""Active KB version + build_seq lookup against the kb-builder-owned registry.

No ORM model import across the service boundary: the query names the table
and columns pinned in docs/contracts/postgres-knowledge-registry.md /
version-membership.md, and the contract test asserts these constants stay in
sync with those documents. The partial unique index on kb_build_run guarantees
at most one 'active' row.

The broker serves by INTERVAL MEMBERSHIP, not kb_version label-equality
(version-membership.md, ADR-0013): it resolves the active build's `build_seq`
once and filters every artifact/edge/provenance/graph/search query by the
membership predicate against this `build_seq`. `kb_version` stays as a label only.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KB_BUILD_RUN_TABLE = "kb_build_run"
KB_VERSION_COLUMN = "kb_version"
BUILD_SEQ_COLUMN = "build_seq"
STATUS_COLUMN = "status"

_ACTIVE_VERSION_QUERY = text(
    f"SELECT {KB_VERSION_COLUMN}, {BUILD_SEQ_COLUMN} FROM {KB_BUILD_RUN_TABLE} "
    f"WHERE {STATUS_COLUMN} = 'active'"
)


@dataclass(frozen=True)
class ActiveVersion:
    """The served version label plus its interval-membership cutoff `build_seq`."""

    kb_version: str
    build_seq: int


async def fetch_active_version(session: AsyncSession) -> ActiveVersion | None:
    """Resolve the active build's (kb_version, build_seq), or None if no active build."""
    row = (await session.execute(_ACTIVE_VERSION_QUERY)).one_or_none()
    if row is None:
        return None
    return ActiveVersion(kb_version=row.kb_version, build_seq=row.build_seq)


async def fetch_active_kb_version(session: AsyncSession) -> str | None:
    """The active kb_version label only (back-compat for callers that just log it)."""
    active = await fetch_active_version(session)
    return active.kb_version if active is not None else None
