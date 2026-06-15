"""Cross-domain candidate generator, DB-backed (PR-28, ADR-0010 phase 3A).

Covers the brief's DB-level acceptance criteria:
- candidates land in `relationship_candidate` with their signals;
- `knowledge_edge` is provably UNTOUCHED (count before == after) — the generator
  writes NO edges and calls NO LLM;
- fan-out per `from` artifact is bounded (<= CANDIDATE_FAN_OUT_K);
- the generator is idempotent: a re-run on unchanged inputs inserts/prunes nothing;
- deterministically-linked pairs are excluded (the judge never re-judges a fact).

Mirrors the fixture style of test_cross_domain_build.py. Skipped gracefully when
TEST_DATABASE_URL is not configured.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application.write_commit import COMMIT_ARTIFACT_TYPE
from agentic_kb_builder.connectors.git_metadata import CHANGED_FILES_HEADER
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    KnowledgeEdge,
    RelationshipCandidate,
    SourceItem,
)
from agentic_kb_builder.linker.candidates import CANDIDATE_FAN_OUT_K
from agentic_kb_builder.linker.run import run_linker
from agentic_kb_builder.linker.run_candidates import run_candidate_generator

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None, reason="no test database configured (set TEST_DATABASE_URL)"
)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

TABLES_IN_DELETE_ORDER = (
    "relationship_candidate",
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


async def _add_source(
    session: AsyncSession,
    *,
    source_type: str,
    source_uri: str,
    path: str | None = None,
    external_id: str | None = None,
    branch: str | None = None,
) -> SourceItem:
    source = SourceItem(
        source_type=source_type,
        source_uri=source_uri,
        source_version="1",
        path=path,
        external_id=external_id,
        branch=branch,
        content_hash=f"hash:{source_uri}",
        acl_teams=[],
    )
    session.add(source)
    await session.flush()
    return source


async def _add_artifact(
    session: AsyncSession,
    *,
    source: SourceItem,
    artifact_type: str,
    title: str | None,
    body_text: str | None,
    kb_version: str = "v-build.1",
) -> KnowledgeArtifact:
    artifact = KnowledgeArtifact(
        artifact_type=artifact_type,
        source_id=source.source_id,
        title=title,
        body_text=body_text,
        kb_version=kb_version,
    )
    session.add(artifact)
    await session.flush()
    return artifact


def _commit_body(subject: str, files: list[str]) -> str:
    return "\n\n".join([subject, "\n".join([CHANGED_FILES_HEADER, *files])])


async def _seed_cross_domain_chain(session: AsyncSession) -> dict[str, KnowledgeArtifact]:
    code_source = await _add_source(
        session,
        source_type="github_code",
        source_uri="gh://repo/src/payment_service.py",
        path="src/payment_service.py",
    )
    code_file = await _add_artifact(
        session,
        source=code_source,
        artifact_type="code_file",
        title="src/payment_service.py",
        body_text="def charge(): ...",
    )
    card_source = await _add_source(
        session, source_type="ado_card", source_uri="ado://card/4321", external_id="4321"
    )
    card = await _add_artifact(
        session,
        source=card_source,
        artifact_type="summary",
        title="Card 4321: payment service rollout",
        body_text="Ship the payment service rollout in src/payment_service.py.",
    )
    doc_source = await _add_source(
        session, source_type="github_doc", source_uri="gh://repo/docs/design.md"
    )
    doc = await _add_artifact(
        session,
        source=doc_source,
        artifact_type="summary",
        title="payment service design",
        body_text="The payment service rollout is described here.",
    )
    commit_source = await _add_source(
        session,
        source_type="git_metadata",
        source_uri="git:c0ffee0",
        external_id="c0ffee0",
        branch="feature/AB-4321-svc",
    )
    await _add_artifact(
        session,
        source=commit_source,
        artifact_type=COMMIT_ARTIFACT_TYPE,
        title="c0ffee0",
        body_text=_commit_body("AB#4321 add payment service", ["src/payment_service.py"]),
    )
    return {"code_file": code_file, "card": card, "doc": doc}


async def _edge_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(KnowledgeEdge))).scalar_one()


async def _candidate_pairs(session: AsyncSession) -> set[frozenset[object]]:
    rows = await session.execute(
        select(RelationshipCandidate.from_artifact_id, RelationshipCandidate.to_artifact_id)
    )
    return {frozenset((frm, to)) for frm, to in rows.tuples()}


@requires_db
async def test_candidates_written_and_edges_untouched(session: AsyncSession) -> None:
    await _seed_cross_domain_chain(session)
    # deterministic links first (creates the edges the generator must NOT touch).
    await run_linker(session, kb_version="v-link.1")
    edges_before = await _edge_count(session)
    assert edges_before > 0  # the linker produced deterministic cross-domain edges

    inserted, refreshed, pruned = await run_candidate_generator(session, kb_version="v-cand.1")
    assert inserted > 0
    assert refreshed == 0
    assert pruned == 0

    # knowledge_edge is provably UNTOUCHED.
    assert await _edge_count(session) == edges_before

    # candidates landed with non-empty signals and a recall bucket.
    rows = await session.execute(
        select(
            RelationshipCandidate.signals,
            RelationshipCandidate.candidate_recall_bucket,
            RelationshipCandidate.kb_version,
        )
    )
    materialised = rows.all()
    assert materialised
    for signals, bucket, kb_version in materialised:
        assert signals  # every candidate records its firing signals
        assert bucket in {"high", "medium", "low"}
        assert kb_version == "v-cand.1"


@requires_db
async def test_already_linked_pair_is_not_a_candidate(session: AsyncSession) -> None:
    chain = await _seed_cross_domain_chain(session)
    await run_linker(session, kb_version="v-link.1")
    await run_candidate_generator(session, kb_version="v-cand.1")

    pairs = await _candidate_pairs(session)

    # The doc↔card pair is NOT deterministically linked (the doc names no work-item
    # id verbatim) but the two share tokens — a genuine cross-domain candidate.
    doc_card = frozenset((chain["doc"].artifact_id, chain["card"].artifact_id))
    assert doc_card in pairs

    # No candidate duplicates a LIVE deterministic linker edge — a pair the linker
    # already linked (e.g. the card names the code path ⇒ card mentions code_file) is
    # a settled fact the judge never re-judges, so it is excluded from candidates.
    live_edges = await session.execute(
        select(KnowledgeEdge.from_artifact_id, KnowledgeEdge.to_artifact_id).where(
            KnowledgeEdge.source == "linker", KnowledgeEdge.invalidated_at_seq.is_(None)
        )
    )
    linked = {frozenset((frm, to)) for frm, to in live_edges.tuples()}
    assert linked  # the linker produced at least one cross-domain edge
    assert pairs.isdisjoint(linked)


@requires_db
async def test_candidate_generation_is_idempotent(session: AsyncSession) -> None:
    await _seed_cross_domain_chain(session)
    await run_linker(session, kb_version="v-link.1")
    first = await run_candidate_generator(session, kb_version="v-cand.1")
    assert first[0] > 0
    count_after_first = (
        await session.execute(select(func.count()).select_from(RelationshipCandidate))
    ).scalar_one()

    # re-run on unchanged inputs: zero churn (refreshes in place, prunes nothing).
    inserted, _refreshed, pruned = await run_candidate_generator(session, kb_version="v-cand.1")
    assert inserted == 0
    assert pruned == 0
    count_after_second = (
        await session.execute(select(func.count()).select_from(RelationshipCandidate))
    ).scalar_one()
    assert count_after_first == count_after_second


@requires_db
async def test_fan_out_is_bounded_in_db(session: AsyncSession) -> None:
    # one card overlapping many code files: candidates from the card must be <= K.
    card_source = await _add_source(
        session, source_type="ado_card", source_uri="ado://card/9001", external_id="9001"
    )
    card = await _add_artifact(
        session,
        source=card_source,
        artifact_type="summary",
        title="payment service rollout module",
        body_text="payment service rollout module across files",
    )
    for i in range(CANDIDATE_FAN_OUT_K + 6):
        code_source = await _add_source(
            session,
            source_type="github_code",
            source_uri=f"gh://repo/src/payment_service_{i}.py",
            path=f"src/payment_service_{i}.py",
        )
        await _add_artifact(
            session,
            source=code_source,
            artifact_type="code_file",
            title=f"src/payment_service_{i}.py",
            body_text="def f(): ...",
        )
    await run_candidate_generator(session, kb_version="v-cand.1")
    from_card = (
        await session.execute(
            select(func.count())
            .select_from(RelationshipCandidate)
            .where(RelationshipCandidate.from_artifact_id == card.artifact_id)
        )
    ).scalar_one()
    assert from_card <= CANDIDATE_FAN_OUT_K
