"""trust_class column (PR-23 / ADR-0011): migration up/down + writer defaults.

The migration adds knowledge_edge.trust_class with a CHECK over the bucket set
and a (kb_version, trust_class) index, backfilling existing rows to EXTRACTED
via the server default. Downgrade reverses all three. The deterministic edge
writers (graphify, linker) may ONLY ever assign EXTRACTED
(docs/contracts/trust-buckets.md); these tests pin that.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.domain import CodeArtifactDraft, CodeEdgeDraft, LinkEdgeDraft
from agentic_kb_builder.graphify.write import write_code_artifacts, write_code_edges
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)
from agentic_kb_builder.linker.write_edges import write_link_edges

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None, reason="no test database configured (set TEST_DATABASE_URL)"
)

KB_VERSION = "kb-trust-test"
_CHECK_NAME = "ck_knowledge_edge_trust_class"
_INDEX_NAME = "ix_knowledge_edge_kb_version_trust_class"


def _run_alembic(target: str) -> None:
    """Run an alembic up/downgrade. alembic's env.py calls asyncio.run(), so
    callers in an async test must dispatch this via asyncio.to_thread to avoid
    nesting event loops."""
    assert TEST_DATABASE_URL is not None
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    cfg = Config(str(ALEMBIC_INI))
    try:
        if target == "0008":
            command.downgrade(cfg, "0008")
        else:
            command.upgrade(cfg, target)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


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
        yield sess
        await sess.rollback()
        await sess.execute(text("DELETE FROM knowledge_edge"))
        await sess.execute(text("DELETE FROM knowledge_artifact"))
        await sess.execute(text("DELETE FROM source_item"))
        await sess.commit()
    await engine.dispose()


async def _seed_source_and_artifacts(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    source = SourceItem(
        source_type="github_code",
        source_uri="repo://trust-test",
        source_version="1",
        content_hash="hash:trust-test",
        is_deleted=False,
    )
    session.add(source)
    await session.flush()
    a = KnowledgeArtifact(
        artifact_type="code_symbol",
        source_id=source.source_id,
        title="symbol A",
        body_text="a",
        kb_version=KB_VERSION,
        knowledge_kind="source_backed",
        authority_score=1.0,
        freshness_score=1.0,
    )
    b = KnowledgeArtifact(
        artifact_type="code_symbol",
        source_id=source.source_id,
        title="symbol B",
        body_text="b",
        kb_version=KB_VERSION,
        knowledge_kind="source_backed",
        authority_score=1.0,
        freshness_score=1.0,
    )
    session.add_all([a, b])
    await session.flush()
    return source.source_id, a.artifact_id, b.artifact_id


# ---------------------------------------------------------------------------
# Migration up / down
# ---------------------------------------------------------------------------


async def _introspect_trust_class() -> tuple[bool, bool, bool]:
    """Query Postgres catalogs for (column, index, check) on knowledge_edge."""
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            has_column = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns WHERE"
                        " table_name = 'knowledge_edge' AND column_name = 'trust_class'"
                    )
                )
            ).first() is not None
            has_index = (
                await conn.execute(
                    text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
                    {"name": _INDEX_NAME},
                )
            ).first() is not None
            has_check = (
                await conn.execute(
                    text("SELECT 1 FROM pg_constraint WHERE conname = :name AND contype = 'c'"),
                    {"name": _CHECK_NAME},
                )
            ).first() is not None
    finally:
        await engine.dispose()
    return has_column, has_index, has_check


@requires_db
async def test_migration_head_adds_column_check_and_index(migrated_db: None) -> None:
    has_column, has_index, has_check = await _introspect_trust_class()
    assert has_column, "trust_class column missing after upgrade head"
    assert has_index, "(kb_version, trust_class) index missing after upgrade head"
    assert has_check, "trust_class CHECK constraint missing after upgrade head"


@requires_db
async def test_downgrade_to_0008_drops_column_check_and_index() -> None:
    try:
        await asyncio.to_thread(_run_alembic, "head")
        await asyncio.to_thread(_run_alembic, "0008")
        has_column, has_index, has_check = await _introspect_trust_class()
        assert not has_column, "trust_class column survived downgrade"
        assert not has_index, "trust_class index survived downgrade"
        assert not has_check, "trust_class CHECK survived downgrade"
    finally:
        # restore the schema for later tests / module fixtures regardless of outcome
        await asyncio.to_thread(_run_alembic, "head")


@requires_db
async def test_check_constraint_rejects_unknown_bucket(session: AsyncSession) -> None:
    _, a, b = await _seed_source_and_artifacts(session)
    with pytest.raises(Exception, match=_CHECK_NAME):
        await session.execute(
            text(
                "INSERT INTO knowledge_edge (from_artifact_id, to_artifact_id, edge_type,"
                " source, kb_version, trust_class) VALUES (CAST(:f AS uuid), CAST(:t AS uuid),"
                " 'calls', 'graphify', :kb, 'NONSENSE')"
            ),
            {"f": str(a), "t": str(b), "kb": KB_VERSION},
        )


# ---------------------------------------------------------------------------
# Edge writers assign EXTRACTED
# ---------------------------------------------------------------------------


@requires_db
async def test_graphify_writer_assigns_extracted(session: AsyncSession) -> None:
    source_id, _, _ = await _seed_source_and_artifacts(session)
    # Build two symbol artifacts via the writer so the edge resolver, which
    # consults key_to_id first, can resolve the edge endpoints in-batch.
    key_to_id = await write_code_artifacts(
        session,
        source_id=source_id,
        kb_version=KB_VERSION,
        drafts=[
            CodeArtifactDraft(
                key="sym:trust.py::a",
                artifact_type="code_symbol",
                title="a",
                body_text="a",
                span_start=1,
                span_end=1,
            ),
            CodeArtifactDraft(
                key="sym:trust.py::b",
                artifact_type="code_symbol",
                title="b",
                body_text="b",
                span_start=1,
                span_end=1,
            ),
        ],
    )
    repo = "trust-repo"
    inserted, dropped = await write_code_edges(
        session,
        kb_version=KB_VERSION,
        repo=repo,
        drafts=[
            CodeEdgeDraft(
                from_key="sym:trust.py::a",
                to_key="sym:trust.py::b",
                edge_type="calls",
                confidence=1.0,
            )
        ],
        key_to_id={
            (repo, "sym:trust.py::a"): key_to_id["sym:trust.py::a"],
            (repo, "sym:trust.py::b"): key_to_id["sym:trust.py::b"],
        },
    )
    assert (inserted, dropped) == (1, 0)
    trust_classes = (
        (
            await session.execute(
                select(KnowledgeEdge.trust_class).where(KnowledgeEdge.source == "graphify")
            )
        )
        .scalars()
        .all()
    )
    assert trust_classes == ["EXTRACTED"]


@requires_db
async def test_linker_writer_assigns_extracted(session: AsyncSession) -> None:
    _, a, b = await _seed_source_and_artifacts(session)
    inserted, refreshed, deleted = await write_link_edges(
        session,
        kb_version=KB_VERSION,
        drafts=[
            LinkEdgeDraft(
                from_artifact_id=a,
                to_artifact_id=b,
                edge_type="documents",
                confidence=1.0,
                strategy="deterministic",
            )
        ],
    )
    assert (inserted, refreshed, deleted) == (1, 0, 0)
    trust_classes = (
        (
            await session.execute(
                select(KnowledgeEdge.trust_class).where(KnowledgeEdge.source == "linker")
            )
        )
        .scalars()
        .all()
    )
    assert trust_classes == ["EXTRACTED"]
