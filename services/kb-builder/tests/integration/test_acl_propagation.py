"""Derived knowledge artifacts default to org-public acl_teams (KB-5 / #25).

The wikify and graphify writers construct KnowledgeArtifact rows WITHOUT acl_teams,
so derived artifacts inherit the server default (empty array = org-public). Propagating
source_item.acl_teams onto them is a recorded follow-up
(docs/contracts/postgres-knowledge-registry.md); this pins the current behavior so the
gap stays visible — and a future propagation change deliberately flips this test.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
async def test_derived_artifact_defaults_to_org_public_acl(migrated_db: None) -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            source = SourceItem(
                source_type="github_doc",
                source_uri="repo://acl-propagation-test",
                source_version="1",
                content_hash="hash:acl-propagation-test",
                is_deleted=False,
            )
            session.add(source)
            await session.flush()
            # mirrors write_wikify_artifacts / write_code_artifacts: acl_teams unset
            artifact = KnowledgeArtifact(
                artifact_type="summary",
                source_id=source.source_id,
                title="derived",
                body_text="derived body",
                kb_version="kb-acl-test",
                knowledge_kind="interpreted",
                authority_score=0.8,
                freshness_score=1.0,
            )
            session.add(artifact)
            await session.flush()
            stored = (
                await session.execute(
                    select(KnowledgeArtifact.acl_teams).where(
                        KnowledgeArtifact.artifact_id == artifact.artifact_id
                    )
                )
            ).scalar_one()
            assert list(stored) == [], "derived artifacts must default to org-public (empty acl)"
            await session.rollback()
    finally:
        await engine.dispose()
