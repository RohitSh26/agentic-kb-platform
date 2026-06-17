"""Identity-over-time invalidation at build level (PR-27, ADR-0013).

Drives the full BuildRunner across builds and asserts the invalidation pass:
deletion sweep, rename detection (deterministic content_hash), and idempotency.
Membership is read back via the predicate (valid_from_seq / invalidated_at_seq)
against each build's build_seq, exactly as mcp-server serves it.

Skips when TEST_DATABASE_URL is unset (shared-DB policy, same as the engine tests).
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application import BuildRunner, EmbeddingResult
from agentic_kb_builder.application.invalidation import run_invalidation_pass
from agentic_kb_builder.connectors import GitHubDocConnector
from agentic_kb_builder.domain import NormalizedContent, SourceRef, WikifyArtifactDraft
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import (
    EmbeddingCache,
    GenerationCacheArtifact,
    KbBuildRun,
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

TABLES_IN_DELETE_ORDER = (
    "retrieval_event",
    "embedding_cache",
    "generation_cache_artifact",
    "generation_cache",
    "knowledge_edge",
    "knowledge_artifact",
    "source_item",
    "kb_build_run",
)


@pytest.fixture(scope="module")
def migrated_db() -> Iterator[None]:
    assert TEST_DATABASE_URL is not None
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    yield
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
        for table in TABLES_IN_DELETE_ORDER:
            await sess.execute(text(f"DELETE FROM {table}"))
        await sess.commit()
        yield sess
        await sess.rollback()
        for table in TABLES_IN_DELETE_ORDER:
            await sess.execute(text(f"DELETE FROM {table}"))
        await sess.commit()
    await engine.dispose()


class _Wikifier:
    model_name = "gpt-test"
    model_params_hash = "params-test"

    def __init__(self) -> None:
        self.calls = 0

    async def wikify(self, content: NormalizedContent) -> Sequence[WikifyArtifactDraft]:
        self.calls += 1
        # body_text is the source text so the artifact content_hash tracks the
        # source content — the deterministic signal the rename pass keys on.
        return [
            WikifyArtifactDraft(
                artifact_type="summary",
                knowledge_kind="interpreted",
                title=f"summary of {content.source.path}",
                body_text=content.text,
                authority_score=0.5,
                freshness_score=1.0,
            )
        ]


class _Embedder:
    embedding_model = "embed-test"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(embedding_hash="emb-" + content_hash(text)[:12], vector=[0.5, 0.25])


class _Indexer:
    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int:
        return len(artifact_ids)

    async def delete_orphaned(self) -> int:
        return 0

    async def reconcile_missing(self) -> int:
        return 0


def _ref(path: str) -> SourceRef:
    return SourceRef(
        source_type="github_doc",
        source_uri=f"repo://doc/{path}",
        source_version="rev-1",
        repo="o/r",
        path=path,
        branch="main",
    )


class _Backend:
    def __init__(self, paths_to_text: dict[str, str]) -> None:
        self._refs = [_ref(p) for p in paths_to_text]
        self._texts = {f"repo://doc/{p}": t for p, t in paths_to_text.items()}

    async def list_sources(self) -> list[SourceRef]:
        return list(self._refs)

    async def fetch_text(self, source: SourceRef) -> str:
        return self._texts[source.source_uri]


def _connector(paths_to_text: dict[str, str]) -> GitHubDocConnector:
    # github_doc ⇒ wikify only, no graphify (the runner graphifies github_code).
    return GitHubDocConnector(_Backend(paths_to_text))


def _runner(session: AsyncSession, kb_version: str) -> BuildRunner:
    return BuildRunner(
        session,
        kb_version=kb_version,
        wikifier=_Wikifier(),
        embedder=_Embedder(),
        indexer=_Indexer(),
    )


async def _members(session: AsyncSession, seq: int) -> set[uuid.UUID]:
    """Artifact ids that are MEMBERS of version `seq` (the served set)."""
    rows = await session.execute(
        select(KnowledgeArtifact.artifact_id).where(
            KnowledgeArtifact.valid_from_seq <= seq,
            (KnowledgeArtifact.invalidated_at_seq.is_(None))
            | (KnowledgeArtifact.invalidated_at_seq > seq),
        )
    )
    return {r.artifact_id for r in rows}


@requires_db
async def test_incremental_build_serves_all_sources_not_just_the_delta(
    session: AsyncSession,
) -> None:
    """THE bug fix at build level: v1 builds 3 docs; v2 changes 1 (2 cache hits).
    Every live artifact is a member of v2's build_seq — not only the changed one."""
    runner1 = _runner(session, "kb-v1")
    run1 = await runner1.run([_connector({"a.md": "alpha", "b.md": "beta", "c.md": "gamma"})])
    await session.commit()
    assert run1.sources_changed == 3

    runner2 = _runner(session, "kb-v2")
    run2 = await runner2.run([_connector({"a.md": "alpha", "b.md": "beta", "c.md": "gamma-NEW"})])
    await session.commit()
    assert run2.sources_changed == 1  # only c.md changed (2 cache hits)

    members_v2 = await _members(session, run2.build_seq)
    # all THREE docs' summaries are served by v2, even though 2 were cache hits.
    assert len(members_v2) == 3


@requires_db
async def test_deleted_source_invalidated_in_new_version_prior_still_serves(
    session: AsyncSession,
) -> None:
    runner1 = _runner(session, "kb-v1")
    run1 = await runner1.run([_connector({"a.md": "alpha", "b.md": "beta"})])
    await session.commit()
    members_v1 = await _members(session, run1.build_seq)
    assert len(members_v1) == 2

    # v2 drops b.md from the listing entirely.
    runner2 = _runner(session, "kb-v2")
    run2 = await runner2.run([_connector({"a.md": "alpha"})])
    await session.commit()

    members_v2 = await _members(session, run2.build_seq)
    # b.md's summary is invalidated in v2 (not served) ...
    assert len(members_v2) == 1
    # ... but v1 (prior) STILL serves both (immutability, invariant 5).
    assert await _members(session, run1.build_seq) == members_v1

    # b.md's source is marked deleted, and its cache rows are retired.
    b_source = (
        await session.execute(select(SourceItem).where(SourceItem.source_uri == "repo://doc/b.md"))
    ).scalar_one()
    assert b_source.is_deleted is True
    # no embedding_cache / generation_cache_artifact row points at an invalidated
    # b.md artifact (retired by the deletion sweep).
    invalid_ids = {
        r.artifact_id
        for r in await session.execute(
            select(KnowledgeArtifact.artifact_id).where(
                KnowledgeArtifact.invalidated_at_seq.is_not(None)
            )
        )
    }
    assert invalid_ids
    emb = await session.execute(
        select(func.count())
        .select_from(EmbeddingCache)
        .where(EmbeddingCache.artifact_id.in_(invalid_ids))
    )
    gca = await session.execute(
        select(func.count())
        .select_from(GenerationCacheArtifact)
        .where(GenerationCacheArtifact.artifact_id.in_(invalid_ids))
    )
    assert emb.scalar_one() == 0
    assert gca.scalar_one() == 0


@requires_db
async def test_rename_links_identity_reattaches_edges_and_invalidates_old(
    session: AsyncSession,
) -> None:
    """Deterministic rename: a vanished source's artifact whose content_hash
    reappears at a NEW source this build ⇒ the new artifact carries
    prior_identity_id, live edges reattach old -> new, the old is invalidated, and
    no ghost remains. Driven directly through the invalidation pass with the
    artifact shapes a renamed CODE file produces (graphify keys on path, so a
    content-identical rename is a cache MISS that writes a new artifact — exactly
    the signal the pass keys on). Doc/wikify renames are a content-keyed cache hit
    that reuses the same artifact and so need no reattach; that is flagged as a
    known scope boundary in the PR notes."""
    same_hash = content_hash("renamed body")
    old_source = uuid.uuid4()
    new_source = uuid.uuid4()
    for sid, path in ((old_source, "old/path.py"), (new_source, "new/path.py")):
        await session.execute(
            text(
                "INSERT INTO source_item (source_id, source_type, source_uri,"
                " source_version, content_hash, path, repo, is_deleted) VALUES"
                " (CAST(:sid AS uuid), 'github_code', :uri, 'rev', :ch, :path, 'o/r', false)"
            ),
            {"sid": str(sid), "uri": f"repo://{path}", "ch": "sh", "path": path},
        )
    # old artifact (introduced in build 1), plus a neighbor edge pointing at it.
    old_art = KnowledgeArtifact(
        artifact_type="code_file",
        source_id=old_source,
        title="old/path.py",
        body_text="renamed body",
        content_hash=same_hash,
        kb_version="kb-v1",
        valid_from_seq=1,
        knowledge_kind="source_backed",
    )
    neighbor = KnowledgeArtifact(
        artifact_type="code_symbol",
        source_id=new_source,
        title="caller",
        body_text="x",
        content_hash="other",
        kb_version="kb-v2",
        valid_from_seq=2,
        knowledge_kind="source_backed",
    )
    # the NEW artifact, introduced THIS build (seq 2) at the new path, same hash.
    new_art = KnowledgeArtifact(
        artifact_type="code_file",
        source_id=new_source,
        title="new/path.py",
        body_text="renamed body",
        content_hash=same_hash,
        kb_version="kb-v2",
        valid_from_seq=2,
        knowledge_kind="source_backed",
    )
    session.add_all([old_art, neighbor, new_art])
    await session.flush()
    edge = KnowledgeEdge(
        from_artifact_id=neighbor.artifact_id,
        to_artifact_id=old_art.artifact_id,
        edge_type="calls",
        confidence=1.0,
        source="graphify",
        kb_version="kb-v1",
        valid_from_seq=1,
        trust_class="EXTRACTED",
    )
    session.add(edge)
    await session.flush()

    # old_source vanished (only new_source seen this build), new_source is changed.
    result = await run_invalidation_pass(
        session,
        build_seq=2,
        seen_source_ids={new_source},
        changed_source_ids={new_source},
    )

    assert result.renames_detected == 1
    assert result.edges_reattached == 1
    await session.refresh(new_art)
    await session.refresh(old_art)
    await session.refresh(edge)
    # history survives via the rename link.
    assert new_art.prior_identity_id == old_art.artifact_id
    # the old artifact is invalidated in v2 (not a member of seq 2) ...
    assert old_art.artifact_id not in await _members(session, 2)
    # ... and the new one IS a member.
    assert new_art.artifact_id in await _members(session, 2)
    # the edge reattached from old -> new (no ghost endpoint).
    assert edge.to_artifact_id == new_art.artifact_id


@requires_db
async def test_idempotent_rebuild_causes_no_invalidation_churn(session: AsyncSession) -> None:
    runner1 = _runner(session, "kb-v1")
    await runner1.run([_connector({"a.md": "alpha", "b.md": "beta"})])
    await session.commit()

    runner2 = _runner(session, "kb-v2")
    run2 = await runner2.run([_connector({"a.md": "alpha", "b.md": "beta"})])
    await session.commit()

    # No source vanished, no rename, no ACL change ⇒ nothing invalidated.
    invalidated = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeArtifact)
            .where(KnowledgeArtifact.invalidated_at_seq.is_not(None))
        )
    ).scalar_one()
    assert invalidated == 0
    # both still members of v2.
    assert len(await _members(session, run2.build_seq)) == 2
    # no source marked deleted.
    deleted = (
        await session.execute(
            select(func.count()).select_from(SourceItem).where(SourceItem.is_deleted.is_(True))
        )
    ).scalar_one()
    assert deleted == 0


@requires_db
async def test_build_seq_is_monotonic(session: AsyncSession) -> None:
    await _runner(session, "kb-v1").run([_connector({"a.md": "alpha"})])
    await session.commit()
    await _runner(session, "kb-v2").run([_connector({"a.md": "alpha"})])
    await session.commit()
    seqs = [
        r.build_seq
        for r in await session.execute(select(KbBuildRun.build_seq).order_by(KbBuildRun.started_at))
    ]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # unique
