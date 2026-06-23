"""Graph-centrality reads the live graph via interval membership (ADR-0028 / PR-36).

The load-bearing concern: an incremental build rewrites only changed sources, so the live graph at
the active build_seq includes STILL-VALID edges from PRIOR builds. Centrality must rank over that
whole live set (interval-membership predicate), not just this build's new edges — else a one-file
build would zero almost every score. This proves earlier-build_seq edges participate.

DB-backed; skipped without TEST_DATABASE_URL. Never run against the :55432 demo DB.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application.centrality import run_centrality
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None, reason="no test database configured (set TEST_DATABASE_URL)"
)
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
_TABLES = ("knowledge_edge", "knowledge_artifact", "source_item", "kb_build_run")


@pytest.fixture(scope="module")
def migrated_db() -> Iterator[None]:
    assert TEST_DATABASE_URL is not None
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous


@pytest.fixture
async def session(migrated_db: None) -> AsyncIterator[AsyncSession]:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        for tbl in _TABLES:
            await sess.execute(text(f"DELETE FROM {tbl}"))
        await sess.commit()
        yield sess
        await sess.rollback()
        for tbl in _TABLES:
            await sess.execute(text(f"DELETE FROM {tbl}"))
        await sess.commit()
    await engine.dispose()


async def _artifact(session: AsyncSession, source_id: uuid.UUID, title: str) -> uuid.UUID:
    row = KnowledgeArtifact(
        artifact_type="code_symbol",
        source_id=source_id,
        title=title,
        body_text=title,
        kb_version="v-test",
        valid_from_seq=1,  # introduced by build_seq 1
        acl_teams=[],
    )
    session.add(row)
    await session.flush()
    return row.artifact_id


@requires_db
async def test_prior_build_edges_participate_in_centrality(session: AsyncSession) -> None:
    src = SourceItem(
        source_type="github_code",
        source_uri="https://example/repo/a.py",
        source_version="sha1",
        content_hash="h1",
    )
    session.add(src)
    await session.flush()

    hub = await _artifact(session, src.source_id, "hub")
    ref1 = await _artifact(session, src.source_id, "ref1")
    ref2 = await _artifact(session, src.source_id, "ref2")
    leaf = await _artifact(session, src.source_id, "leaf")
    # edges introduced at build_seq 1, still members (invalidated_at_seq NULL): ref1,ref2 -> hub
    for frm in (ref1, ref2):
        session.add(
            KnowledgeEdge(
                from_artifact_id=frm,
                to_artifact_id=hub,
                edge_type="calls",
                kb_version="v-test",
                valid_from_seq=1,
            )
        )
    await session.flush()

    # run centrality at a LATER build_seq (2): the seq-1 edges are still live members.
    await run_centrality(session, build_seq=2)
    await session.flush()

    result = (
        await session.execute(
            select(KnowledgeArtifact.artifact_id, KnowledgeArtifact.centrality_score)
        )
    ).all()
    rows: dict[uuid.UUID, float | None] = {r.artifact_id: r.centrality_score for r in result}
    hub_score = rows[hub]
    assert hub_score is not None
    assert hub_score == 1.0  # the most-referenced node is the peak
    assert hub_score > (rows[leaf] or 0.0)  # the unreferenced leaf ranks below the hub
