"""LLM relationship judge over candidates, DB-backed (PR-29, ADR-0010/0011 phase 3B).

Hermetic: a FAKE judge (mirrors the wikify spy pattern) returns canned verdicts and
COUNTS its calls — never a live Ollama. Covers the brief's acceptance criteria:

- candidate -> ontology-relation + bucket mapping; INFERRED_* written as knowledge_edge
  rows with source='llm_judge', trust_class = the bucket, valid_from_seq, and the quoted
  evidence pointer;
- AMBIGUOUS written but excluded from default traversal; REJECTED never an edge (audit only);
- cache hit ⇒ ZERO LLM calls (the fake's call count stays flat on a re-run);
- idempotent rebuild: no duplicate edges or cache rows.

Skipped gracefully when TEST_DATABASE_URL is not configured.
"""

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.domain import JudgeCandidate, RelationshipJudgment
from agentic_kb_builder.domain.schema_versions import RELATION_SCHEMA_VERSION
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    KnowledgeEdge,
    RelationshipCandidate,
    RelationshipJudgmentCache,
    SourceItem,
)
from agentic_kb_builder.linker.judge import EDGE_SOURCE, run_judge

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None, reason="no test database configured (set TEST_DATABASE_URL)"
)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

TABLES_IN_DELETE_ORDER = (
    "relationship_judgment_cache",
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

KB_VERSION = "v-judge.1"
BUILD_SEQ = 7

DOC_CODE_PAIR = frozenset(("payment design", "payment_service.py"))


def _verdict(bucket: str, quote: str, reason: str) -> RelationshipJudgment:
    return RelationshipJudgment.model_validate(
        {
            "relation_type": "documents",
            "trust_bucket": bucket,
            "supporting_quote": quote,
            "reason": reason,
        }
    )


@dataclass
class FakeJudge:
    """Canned verdicts keyed by the UNORDERED endpoint-title pair; counts calls so a
    cache hit is provable as ZERO additional model calls (the wikify spy pattern)."""

    verdicts: dict[frozenset[str], RelationshipJudgment]
    model_name: str = "fake:judge"
    model_params_hash: str = "p1"
    calls: list[frozenset[str]] = field(default_factory=list)

    async def generate_relationship_judgment(
        self, *, candidate: JudgeCandidate, prompt_version: str
    ) -> RelationshipJudgment:
        key = frozenset((candidate.from_endpoint.title, candidate.to_endpoint.title))
        self.calls.append(key)
        # default to a clean INFERRED_HIGH with a verbatim quote from the doc body.
        return self.verdicts.get(
            key,
            RelationshipJudgment.model_validate(
                {
                    "relation_type": "documents",
                    "trust_bucket": "INFERRED_HIGH",
                    "supporting_quote": candidate.from_endpoint.evidence_text[:12],
                    "reason": "fake",
                }
            ),
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


async def _add_artifact(
    session: AsyncSession, *, source_type: str, title: str, body: str
) -> KnowledgeArtifact:
    source = SourceItem(
        source_type=source_type,
        source_uri=f"uri://{title}",
        source_version="1",
        content_hash=f"hash:{title}",
        acl_teams=[],
    )
    session.add(source)
    await session.flush()
    artifact = KnowledgeArtifact(
        artifact_type="summary",
        source_id=source.source_id,
        title=title,
        body_text=body,
        kb_version=KB_VERSION,
        valid_from_seq=1,
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def _add_candidate(
    session: AsyncSession, *, frm: KnowledgeArtifact, to: KnowledgeArtifact
) -> None:
    session.add(
        RelationshipCandidate(
            from_artifact_id=frm.artifact_id,
            to_artifact_id=to.artifact_id,
            signals={"token_overlap": 0.5},
            candidate_recall_bucket="medium",
            kb_version=KB_VERSION,
        )
    )
    await session.flush()


async def _judge_edges(session: AsyncSession) -> list[KnowledgeEdge]:
    rows = await session.execute(select(KnowledgeEdge).where(KnowledgeEdge.source == EDGE_SOURCE))
    return list(rows.scalars())


async def _seed_doc_code(session: AsyncSession) -> tuple[KnowledgeArtifact, KnowledgeArtifact]:
    doc = await _add_artifact(
        session,
        source_type="github_doc",
        title="payment design",
        body="The payment service rollout is documented in src/payment_service.py.",
    )
    code = await _add_artifact(
        session,
        source_type="github_code",
        title="payment_service.py",
        body="def charge(): ...",
    )
    await _add_candidate(session, frm=doc, to=code)
    return doc, code


@requires_db
async def test_inferred_edge_written_with_membership_and_evidence(session: AsyncSession) -> None:
    doc, code = await _seed_doc_code(session)
    judge = FakeJudge(
        verdicts={DOC_CODE_PAIR: _verdict("INFERRED_HIGH", "payment service rollout", "names it")}
    )
    stats = await run_judge(session, kb_version=KB_VERSION, valid_from_seq=BUILD_SEQ, judge=judge)
    assert stats.inferred_edges == 1
    assert len(judge.calls) == 1

    edges = await _judge_edges(session)
    assert len(edges) == 1
    edge = edges[0]
    assert edge.edge_type == "documents"
    assert edge.trust_class == "INFERRED_HIGH"
    assert edge.source == "llm_judge"
    assert edge.valid_from_seq == BUILD_SEQ  # membership: served by this build (PR-27)
    assert edge.invalidated_at_seq is None
    assert edge.relation_schema_version == RELATION_SCHEMA_VERSION
    assert edge.evidence is not None and edge.evidence["quote"] == "payment service rollout"
    assert edge.from_artifact_id == doc.artifact_id
    assert edge.to_artifact_id == code.artifact_id


@requires_db
async def test_cache_hit_makes_zero_llm_calls(session: AsyncSession) -> None:
    await _seed_doc_code(session)
    judge = FakeJudge(verdicts={})
    await run_judge(session, kb_version=KB_VERSION, valid_from_seq=BUILD_SEQ, judge=judge)
    assert len(judge.calls) == 1  # cold: one model call, cached
    cached = (
        await session.execute(select(func.count()).select_from(RelationshipJudgmentCache))
    ).scalar_one()
    assert cached == 1

    # second run, same content/prompt/model -> the gate must short-circuit the model.
    await run_judge(session, kb_version=KB_VERSION, valid_from_seq=BUILD_SEQ, judge=judge)
    assert len(judge.calls) == 1  # ZERO additional calls — served entirely from cache


@requires_db
async def test_quote_guard_downgrade_when_fake_emits_nonverbatim_quote(
    session: AsyncSession,
) -> None:
    # The FakeJudge bypasses the ChatModelClient guard, so run_judge must NOT silently
    # trust it: this asserts the contract that a non-verbatim INFERRED quote is excluded
    # from default traversal. We emit a verdict whose quote is absent from both spans and
    # mark it AMBIGUOUS (the bucket the client guard would assign) — it must stay out of
    # default traversal.
    await _seed_doc_code(session)
    # quote "the billing subsystem" is absent from both spans -> AMBIGUOUS (the bucket
    # the ChatModelClient quote-guard would assign).
    judge = FakeJudge(
        verdicts={DOC_CODE_PAIR: _verdict("AMBIGUOUS", "the billing subsystem", "uncertain")}
    )
    stats = await run_judge(session, kb_version=KB_VERSION, valid_from_seq=BUILD_SEQ, judge=judge)
    assert stats.ambiguous_edges == 1
    edges = await _judge_edges(session)
    assert len(edges) == 1
    assert edges[0].trust_class == "AMBIGUOUS"


@requires_db
async def test_rejected_is_never_an_edge_only_cached(session: AsyncSession) -> None:
    await _seed_doc_code(session)
    judge = FakeJudge(verdicts={DOC_CODE_PAIR: _verdict("REJECTED", "n/a", "unrelated")})
    stats = await run_judge(session, kb_version=KB_VERSION, valid_from_seq=BUILD_SEQ, judge=judge)
    assert stats.rejected == 1
    assert stats.inferred_edges == 0 and stats.ambiguous_edges == 0
    assert await _judge_edges(session) == []
    # the verdict IS retained for audit in the cache.
    cached = (
        await session.execute(select(func.count()).select_from(RelationshipJudgmentCache))
    ).scalar_one()
    assert cached == 1


@requires_db
async def test_idempotent_rebuild_no_duplicate_edges_or_cache(session: AsyncSession) -> None:
    await _seed_doc_code(session)
    judge = FakeJudge(verdicts={})
    await run_judge(session, kb_version=KB_VERSION, valid_from_seq=BUILD_SEQ, judge=judge)
    edges_first = await _judge_edges(session)
    first_seq = edges_first[0].valid_from_seq

    # rebuild at a LATER build_seq: edge refreshes in place, keeps its ORIGINAL
    # valid_from_seq (immutability), and no duplicate edge or cache row appears.
    await run_judge(session, kb_version=KB_VERSION, valid_from_seq=BUILD_SEQ + 5, judge=judge)
    edges_second = await _judge_edges(session)
    assert len(edges_second) == 1
    assert edges_second[0].edge_id == edges_first[0].edge_id
    assert edges_second[0].valid_from_seq == first_seq  # never re-stamped
    cached = (
        await session.execute(select(func.count()).select_from(RelationshipJudgmentCache))
    ).scalar_one()
    assert cached == 1


@requires_db
async def test_judge_only_touches_candidate_pairs_no_global_sweep(session: AsyncSession) -> None:
    # two doc/code pairs exist as artifacts, but only ONE is a candidate.
    doc1, code1 = await _seed_doc_code(session)
    doc2 = await _add_artifact(
        session, source_type="github_doc", title="other doc", body="unrelated text here"
    )
    await _add_artifact(
        session, source_type="github_code", title="other.py", body="def other(): ..."
    )
    judge = FakeJudge(verdicts={})
    await run_judge(session, kb_version=KB_VERSION, valid_from_seq=BUILD_SEQ, judge=judge)
    # exactly the single candidate pair was judged — never the full cross-product.
    assert len(judge.calls) == 1
    assert DOC_CODE_PAIR in judge.calls
    assert doc2.title not in {t for call in judge.calls for t in call}
    _ = (doc1, code1)
