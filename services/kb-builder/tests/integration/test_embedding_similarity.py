"""EmbeddingSimilarityProvider: nearest-neighbour over code_symbols + cache reuse.

Uses a deterministic FAKE embedder (no Ollama) so the test is hermetic but still
exercises the real cache-gated load + numpy cosine ranking against a migrated DB.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application.build_runner import EmbeddingResult
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import (
    EmbeddingCache,
    KnowledgeArtifact,
    SourceItem,
)
from agentic_kb_builder.linker.embedding_similarity import EmbeddingSimilarityProvider

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None, reason="no test database configured (set TEST_DATABASE_URL)"
)
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

TABLES_IN_DELETE_ORDER = (
    "embedding_cache",
    "knowledge_edge",
    "knowledge_artifact",
    "source_item",
    "kb_build_run",
)


class FakeEmbedder:
    """Deterministic 3-dim embedder: axes = ['auth', 'budget', 'other'] keyword hits."""

    embedding_model = "fake-embed-v1"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text_in: str) -> EmbeddingResult:
        self.calls += 1
        low = text_in.lower()
        vector = [
            1.0 if "auth" in low else 0.0,
            1.0 if "budget" in low else 0.0,
            0.2,
        ]
        return EmbeddingResult(embedding_hash=content_hash(text_in), vector=vector)


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
        for table in TABLES_IN_DELETE_ORDER:
            await sess.execute(text(f"DELETE FROM {table}"))
        await sess.commit()
    await engine.dispose()


async def _add(
    session: AsyncSession, *, artifact_type: str, title: str, body: str | None
) -> KnowledgeArtifact:
    source = SourceItem(
        source_type="github_code",
        source_uri=f"gh://repo/{title}",
        source_version="1",
        content_hash=f"hash:{title}",
    )
    session.add(source)
    await session.flush()
    artifact = KnowledgeArtifact(
        artifact_type=artifact_type,
        source_id=source.source_id,
        title=title,
        body_text=body,
        kb_version="v.1",
    )
    session.add(artifact)
    await session.flush()
    return artifact


@requires_db
async def test_concept_matches_the_semantically_closest_code_symbol(session: AsyncSession) -> None:
    concept = await _add(session, artifact_type="concept", title="Budget enforcement", body=None)
    budget_sym = await _add(
        session,
        artifact_type="code_symbol",
        title="enforce_budget",
        body="def enforce_budget(): ...",
    )
    auth_sym = await _add(
        session, artifact_type="code_symbol", title="validate_auth", body="def validate_auth(): ..."
    )
    await session.commit()

    provider = EmbeddingSimilarityProvider(session, FakeEmbedder())
    scored = await provider.similar_code_symbols(artifact_id=concept.artifact_id, top_k=2)

    assert scored, "expected at least one code_symbol match"
    assert scored[0].artifact_id == budget_sym.artifact_id  # budget concept -> budget symbol
    assert scored[0].similarity > 0.9
    # auth symbol is also returned but ranks below the budget one.
    assert auth_sym.artifact_id in {s.artifact_id for s in scored}


@requires_db
async def test_query_symbol_excludes_itself(session: AsyncSession) -> None:
    sym = await _add(
        session,
        artifact_type="code_symbol",
        title="enforce_budget",
        body="def enforce_budget(): ...",
    )
    other = await _add(
        session, artifact_type="code_symbol", title="budget_helper", body="def budget_helper(): ..."
    )
    await session.commit()

    provider = EmbeddingSimilarityProvider(session, FakeEmbedder())
    scored = await provider.similar_code_symbols(artifact_id=sym.artifact_id, top_k=5)
    ids = {s.artifact_id for s in scored}
    assert sym.artifact_id not in ids  # never returns itself
    assert other.artifact_id in ids


@requires_db
async def test_second_build_reuses_cached_vectors_no_reembed(session: AsyncSession) -> None:
    concept = await _add(session, artifact_type="concept", title="Budget enforcement", body=None)
    await _add(
        session,
        artifact_type="code_symbol",
        title="enforce_budget",
        body="def enforce_budget(): ...",
    )
    await session.commit()

    first = FakeEmbedder()
    await EmbeddingSimilarityProvider(session, first).similar_code_symbols(
        artifact_id=concept.artifact_id, top_k=1
    )
    assert first.calls == 2  # concept + one code_symbol embedded
    cached = (await session.execute(select(func.count()).select_from(EmbeddingCache))).scalar_one()
    assert cached == 2

    # A fresh provider over the same (unchanged) artifacts must hit the cache, not re-embed.
    second = FakeEmbedder()
    await EmbeddingSimilarityProvider(session, second).similar_code_symbols(
        artifact_id=concept.artifact_id, top_k=1
    )
    assert second.calls == 0  # everything served from embedding_cache
