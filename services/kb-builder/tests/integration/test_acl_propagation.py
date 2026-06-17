"""ACL propagation onto derived artifacts (PR-27, ADR-0013 §3).

A derived artifact is visible only where its source is, so kb-builder propagates
source_item.acl_teams onto its derived artifacts at build time:

- the docify/graphify writers stamp the source ACL on NEWLY written artifacts;
- the invalidation pass propagates an ACL-only change (even content-unchanged ⇒
  cache hit) onto a source's live artifacts.

Both are covered here. This replaces the earlier test that PINNED the gap
(derived artifacts defaulting to org-public); ADR-0013 closes it.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_kb_builder.application.invalidation import run_invalidation_pass
from agentic_kb_builder.docify.write import write_doc_artifacts
from agentic_kb_builder.domain import DocArtifactDraft
from agentic_kb_builder.infrastructure.postgres.models import KnowledgeArtifact, SourceItem

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None, reason="no test database configured (set TEST_DATABASE_URL)"
)


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


@requires_db
async def test_writer_propagates_source_acl_onto_new_artifacts(migrated_db: None) -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            source = SourceItem(
                source_type="github_doc",
                source_uri="repo://acl-writer-test",
                source_version="1",
                content_hash="hash:acl-writer-test",
                acl_teams=["team-secure"],
                is_deleted=False,
            )
            session.add(source)
            await session.flush()
            artifact_ids = await write_doc_artifacts(
                session,
                source_id=source.source_id,
                kb_version="kb-acl-writer",
                valid_from_seq=1,
                acl_teams=["team-secure"],
                drafts=[
                    DocArtifactDraft(
                        artifact_type="concept",
                        title="derived",
                        body_text="derived body",
                        knowledge_kind="interpreted",
                        authority_score=0.6,
                        freshness_score=1.0,
                    )
                ],
            )
            stored = (
                await session.execute(
                    select(KnowledgeArtifact.acl_teams).where(
                        KnowledgeArtifact.artifact_id == artifact_ids[0]
                    )
                )
            ).scalar_one()
            assert list(stored) == ["team-secure"], (
                "a newly written derived artifact must inherit its source's acl_teams"
            )
            await session.rollback()
    finally:
        await engine.dispose()


@requires_db
async def test_invalidation_propagates_acl_change_onto_live_artifacts(migrated_db: None) -> None:
    """An ACL-only change (content unchanged ⇒ cache hit) must still land: the
    invalidation pass overwrites the source's live artifacts' acl_teams."""
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            source = SourceItem(
                source_type="github_doc",
                source_uri="repo://acl-prop-test",
                source_version="1",
                content_hash="hash:acl-prop-test",
                acl_teams=["team-restricted"],  # the NEW (tightened) source ACL
                is_deleted=False,
            )
            session.add(source)
            await session.flush()
            # an EXISTING (cache-hit-carried) derived artifact still org-public.
            artifact = KnowledgeArtifact(
                artifact_type="summary",
                source_id=source.source_id,
                title="derived",
                body_text="derived body",
                kb_version="kb-acl-old",
                valid_from_seq=1,
                knowledge_kind="interpreted",
                authority_score=0.8,
                freshness_score=1.0,
                acl_teams=[],
            )
            session.add(artifact)
            await session.flush()

            result = await run_invalidation_pass(
                session, build_seq=2, seen_source_ids={source.source_id}
            )

            assert result.acl_sources_propagated == 1
            assert result.acl_artifacts_updated == 1
            stored = (
                await session.execute(
                    select(KnowledgeArtifact.acl_teams).where(
                        KnowledgeArtifact.artifact_id == artifact.artifact_id
                    )
                )
            ).scalar_one()
            assert list(stored) == ["team-restricted"], (
                "an ACL-only change must propagate onto the source's live artifacts"
            )
            await session.rollback()
    finally:
        await engine.dispose()


@requires_db
async def test_idempotent_acl_propagation_does_not_churn(migrated_db: None) -> None:
    """A rebuild on an unchanged source ACL propagates nothing (no churn)."""
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            source = SourceItem(
                source_type="github_doc",
                source_uri="repo://acl-idem-test",
                source_version="1",
                content_hash="hash:acl-idem-test",
                acl_teams=["team-x"],
                is_deleted=False,
            )
            session.add(source)
            await session.flush()
            artifact = KnowledgeArtifact(
                artifact_type="summary",
                source_id=source.source_id,
                title="derived",
                body_text="derived body",
                kb_version="kb-idem",
                valid_from_seq=1,
                knowledge_kind="interpreted",
                authority_score=0.8,
                freshness_score=1.0,
                acl_teams=["team-x"],  # already matches the source
            )
            session.add(artifact)
            await session.flush()

            result = await run_invalidation_pass(
                session, build_seq=2, seen_source_ids={source.source_id}
            )

            assert result.acl_sources_propagated == 0
            assert result.acl_artifacts_updated == 0
            await session.rollback()
    finally:
        await engine.dispose()
