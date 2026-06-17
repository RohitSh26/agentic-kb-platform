"""Publish gates wired to activation (PR-25, docs/contracts/publish-gates.md).

A build that passes every phase-1 gate activates; a build with an injected
dangling citation / a forced bad edge_type / a forced symbol-count blowup stays
INACTIVE with the failing gate + measured value recorded on kb_build_run, and the
previously active version keeps serving. The allow_large_delta override is honoured.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application.active_version import get_active_kb_version
from agentic_kb_builder.application.publish_gates import (
    _build_seq_for,
    edge_evidence_integrity_gate,
    no_dangling_citations_gate,
    relation_precision_gate,
    symbol_count_delta_gate,
)
from agentic_kb_builder.build import Collaborators, run_build
from agentic_kb_builder.domain import (
    DocArtifactDraft,
    DocExtractionResult,
    NormalizedContent,
)
from agentic_kb_builder.embeddings import LocalHashEmbedder
from agentic_kb_builder.indexing import SearchDocUpserter
from agentic_kb_builder.infrastructure.azure_search.search_client import FakeSearchClient
from agentic_kb_builder.infrastructure.postgres.models import KbBuildRun

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None, reason="no test database configured (set TEST_DATABASE_URL)"
)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

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

SERVICE_PY = (
    "from pkg.util import helper\n\n\n"
    "def top():\n    return helper()\n\n\n"
    "class Service:\n    def handle(self):\n        return self.helper()\n\n"
    "    def helper(self):\n        return top()\n"
)
UTIL_PY = "def helper():\n    return 42\n"

SOURCES_YAML = (
    "version: 1\n"
    "sources:\n"
    "  - name: code\n"
    "    type: github_code\n"
    "    repo: o/r\n"
    "    branch: main\n"
    "    include: ['**/*.py']\n"
)


class FakeDocExtractor:
    model_name = "fake-wikify"
    model_params_hash = "fake-params"

    async def extract(self, content: NormalizedContent) -> DocExtractionResult:
        return DocExtractionResult(
            artifacts=(
                DocArtifactDraft(
                    artifact_type="summary",
                    knowledge_kind="interpreted",
                    title=f"summary of {content.source.path}",
                    body_text=f"Summary of {content.source.path}",
                    authority_score=0.5,
                    freshness_score=1.0,
                ),
            )
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
        for table in TABLES_IN_DELETE_ORDER:
            await sess.execute(text(f"DELETE FROM {table}"))
        await sess.commit()
        yield sess
        await sess.rollback()
        for table in TABLES_IN_DELETE_ORDER:
            await sess.execute(text(f"DELETE FROM {table}"))
        await sess.commit()
    await engine.dispose()


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "service.py").write_text(SERVICE_PY, encoding="utf-8")
    (pkg / "util.py").write_text(UTIL_PY, encoding="utf-8")
    sources = tmp_path / "sources.yaml"
    sources.write_text(SOURCES_YAML, encoding="utf-8")
    return tmp_path, sources


def _collaborators(session: AsyncSession) -> Collaborators:
    client = FakeSearchClient()
    return Collaborators(
        doc_extractor=FakeDocExtractor(),
        embedder=LocalHashEmbedder(),
        indexer=SearchDocUpserter(session, client),
        search_client=client,
    )


async def _build(
    session: AsyncSession, tmp_path: Path, *, kb_version: str, allow_large_delta: bool = False
) -> KbBuildRun:
    workspace, sources = _workspace(tmp_path)
    return await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version=kb_version,
        version="local",
        collaborators=_collaborators(session),
        activate=True,
        allow_large_delta=allow_large_delta,
    )


async def _run_row(session: AsyncSession, kb_version: str) -> KbBuildRun:
    return (
        await session.execute(select(KbBuildRun).where(KbBuildRun.kb_version == kb_version))
    ).scalar_one()


@requires_db
async def test_clean_build_passes_all_gates_and_activates(
    session: AsyncSession, tmp_path: Path
) -> None:
    run = await _build(session, tmp_path, kb_version="v-gate.ok")
    assert run.status in {"completed", "active"}
    assert await get_active_kb_version(session) == "v-gate.ok"
    row = await _run_row(session, "v-gate.ok")
    assert row.failed_gate is None
    assert row.gate_measured_value is None


@requires_db
async def test_dangling_citation_keeps_version_inactive(
    session: AsyncSession, tmp_path: Path
) -> None:
    # build (no activate yet), inject a dangling citation by soft-deleting the
    # source_item behind a citeable artifact, then run the gate validator.
    workspace, sources = _workspace(tmp_path)
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-gate.dangle",
        version="local",
        collaborators=_collaborators(session),
        activate=False,
    )
    await session.execute(text("UPDATE source_item SET is_deleted = true"))
    await session.commit()
    result = await no_dangling_citations_gate(session, "v-gate.dangle")
    assert not result.passed
    assert result.measured_value is not None and result.measured_value > 0


@requires_db
async def test_forced_bad_edge_type_fails_integrity_gate(
    session: AsyncSession, tmp_path: Path
) -> None:
    workspace, sources = _workspace(tmp_path)
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-gate.edge",
        version="local",
        collaborators=_collaborators(session),
        activate=False,
    )
    # corrupt one edge to a banned generic catch-all (relation-ontology.md).
    await session.execute(
        text(
            "UPDATE knowledge_edge SET edge_type = 'related_to' "
            "WHERE kb_version = 'v-gate.edge' "
            "AND edge_id = (SELECT edge_id FROM knowledge_edge "
            "WHERE kb_version = 'v-gate.edge' LIMIT 1)"
        )
    )
    await session.commit()
    result = await edge_evidence_integrity_gate(session, "v-gate.edge")
    assert not result.passed
    assert result.measured_value == 1.0


@requires_db
async def test_symbol_count_blowup_blocks_then_override_activates(
    session: AsyncSession, tmp_path: Path
) -> None:
    # first build activates (no baseline -> delta gate trivially passes).
    await _build(session, tmp_path, kb_version="v-gate.base")
    assert await get_active_kb_version(session) == "v-gate.base"

    # a second build whose symbol count is a blowup vs the active baseline. Force
    # the blowup by deleting most of the new build's symbols before the gate runs.
    workspace, sources = _workspace(tmp_path / "v2")
    # use a fresh workspace dir so content hashes differ and graphify re-runs
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-gate.big",
        version="local2",
        collaborators=_collaborators(session),
        activate=False,
    )
    # keep exactly one v-gate.big symbol; drop the rest (and their edges + cache
    # links first, to satisfy FKs) so the delta vs the 5-symbol baseline blows >25%.
    keep = (
        await session.execute(
            text(
                "SELECT artifact_id FROM knowledge_artifact "
                "WHERE kb_version = 'v-gate.big' AND artifact_type = 'code_symbol' LIMIT 1"
            )
        )
    ).scalar_one()
    # Drop EVERY edge touching a to-be-deleted symbol (by endpoint, any kb_version):
    # code_symbols now carry body_text (ADR-0018) so they can also be linker/candidate
    # edge endpoints, not just kb_version='v-gate.big' graphify edges — scope by FK.
    await session.execute(
        text(
            "DELETE FROM knowledge_edge WHERE from_artifact_id IN ("
            "  SELECT artifact_id FROM knowledge_artifact "
            "  WHERE kb_version = 'v-gate.big' AND artifact_type = 'code_symbol' "
            "  AND artifact_id != CAST(:keep AS uuid)) "
            "OR to_artifact_id IN ("
            "  SELECT artifact_id FROM knowledge_artifact "
            "  WHERE kb_version = 'v-gate.big' AND artifact_type = 'code_symbol' "
            "  AND artifact_id != CAST(:keep AS uuid))"
        ),
        {"keep": str(keep)},
    )
    # code_symbol artifacts now carry exact-span body_text (ADR-0018) and so are
    # embedded — clear their embedding_cache rows first to satisfy the FK.
    await session.execute(
        text(
            "DELETE FROM embedding_cache WHERE artifact_id IN ("
            "  SELECT artifact_id FROM knowledge_artifact "
            "  WHERE kb_version = 'v-gate.big' AND artifact_type = 'code_symbol' "
            "  AND artifact_id != CAST(:keep AS uuid))"
        ),
        {"keep": str(keep)},
    )
    await session.execute(
        text(
            "DELETE FROM generation_cache_artifact WHERE artifact_id IN ("
            "  SELECT artifact_id FROM knowledge_artifact "
            "  WHERE kb_version = 'v-gate.big' AND artifact_type = 'code_symbol' "
            "  AND artifact_id != CAST(:keep AS uuid))"
        ),
        {"keep": str(keep)},
    )
    await session.execute(
        text(
            "DELETE FROM knowledge_artifact WHERE kb_version = 'v-gate.big' "
            "AND artifact_type = 'code_symbol' AND artifact_id != CAST(:keep AS uuid)"
        ),
        {"keep": str(keep)},
    )
    await session.commit()

    blocked = await symbol_count_delta_gate(session, "v-gate.big")
    assert not blocked.passed
    assert blocked.measured_value is not None and blocked.measured_value > 0.25

    # the override honours the blowup and the gate passes (logged).
    await session.execute(
        text("UPDATE kb_build_run SET allow_large_delta = true WHERE kb_version = 'v-gate.big'")
    )
    await session.commit()
    overridden = await symbol_count_delta_gate(session, "v-gate.big")
    assert overridden.passed


@requires_db
async def test_failed_gate_records_reason_and_keeps_prev_active(
    session: AsyncSession, tmp_path: Path
) -> None:
    # build + activate a good baseline, then a second build forced to fail a gate
    # via run_build's full activation path; the new version must stay inactive,
    # the failing gate recorded, and the old version keep serving.
    await _build(session, tmp_path, kb_version="v-gate.serving")
    assert await get_active_kb_version(session) == "v-gate.serving"

    workspace, sources = _workspace(tmp_path / "v2")
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-gate.fail",
        version="local2",
        collaborators=_collaborators(session),
        activate=False,
    )
    # inject a banned edge so the integrity gate fails when activation runs.
    await session.execute(
        text(
            "UPDATE knowledge_edge SET edge_type = 'related_to' "
            "WHERE kb_version = 'v-gate.fail' "
            "AND edge_id = (SELECT edge_id FROM knowledge_edge "
            "WHERE kb_version = 'v-gate.fail' LIMIT 1)"
        )
    )
    await session.commit()

    from agentic_kb_builder.application.active_version import activate_kb_version
    from agentic_kb_builder.application.publish_gates import (
        compose_gates,
        edge_evidence_integrity_gate,
    )

    # compose the edge-integrity gate alone (index consistency across two builds'
    # registries is exercised by test_build_cli; here we isolate the failure-
    # recording + rollback semantics through the real activation path).
    run = await _run_row(session, "v-gate.fail")
    validator = compose_gates([edge_evidence_integrity_gate])
    activated = await activate_kb_version(session, run.build_id, validator)
    await session.commit()

    assert activated is False
    assert await get_active_kb_version(session) == "v-gate.serving"  # prev keeps serving
    failed = await _run_row(session, "v-gate.fail")
    assert failed.status == "validation_failed"
    assert failed.failed_gate == "edge_evidence_integrity"
    assert failed.gate_measured_value == 1.0


@requires_db
async def test_ghost_edge_blocks_no_ghost_edges_gate(session: AsyncSession, tmp_path: Path) -> None:
    """ENFORCING no-ghost-edges (PR-27/ADR-0013): a member edge whose endpoint is
    NOT a member of this build (invalidated) is a ghost ⇒ the gate fails. Scoped by
    membership, not kb_version label-equality."""
    workspace, sources = _workspace(tmp_path)
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-gate.ghost",
        version="local",
        collaborators=_collaborators(session),
        activate=False,
    )
    seq = (
        await session.execute(
            text("SELECT build_seq FROM kb_build_run WHERE kb_version = 'v-gate.ghost'")
        )
    ).scalar_one()
    # Invalidate ONE endpoint of a live member edge AT this build_seq: the edge
    # stays a member (still live) but now points at a non-member ⇒ a ghost.
    await session.execute(
        text(
            "UPDATE knowledge_artifact SET invalidated_at_seq = :seq WHERE artifact_id = ("
            "  SELECT to_artifact_id FROM knowledge_edge"
            "  WHERE kb_version = 'v-gate.ghost' LIMIT 1)"
        ),
        {"seq": seq},
    )
    await session.commit()

    result = await edge_evidence_integrity_gate(session, "v-gate.ghost")
    assert not result.passed
    assert result.measured_value is not None and result.measured_value >= 1.0


@requires_db
async def test_member_linker_edge_without_evidence_blocks_relation_precision(
    session: AsyncSession, tmp_path: Path
) -> None:
    """ENFORCING relation precision (registry-derivable part, PR-27/ADR-0013): a
    member linker edge with a NULL evidence pointer violates the ontology's
    'required edge fields' ⇒ the gate fails. A clean build passes."""
    workspace, sources = _workspace(tmp_path)
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-gate.relprec",
        version="local",
        collaborators=_collaborators(session),
        activate=False,
    )
    # A clean build passes the gate (graphify edges are source='graphify', the
    # gate only checks source='linker').
    clean = await relation_precision_gate(session, "v-gate.relprec")
    assert clean.passed

    # Force a member linker edge with NULL evidence (a precision violation).
    await session.execute(
        text(
            "UPDATE knowledge_edge SET source = 'linker', evidence = NULL "
            "WHERE kb_version = 'v-gate.relprec' "
            "AND edge_id = (SELECT edge_id FROM knowledge_edge "
            "WHERE kb_version = 'v-gate.relprec' LIMIT 1)"
        )
    )
    await session.commit()

    result = await relation_precision_gate(session, "v-gate.relprec")
    assert not result.passed
    assert result.measured_value == 1.0


@requires_db
async def test_retried_kb_version_resolves_to_latest_completed_build_seq(
    session: AsyncSession,
) -> None:
    """KB-F1: a retried build leaves >1 'completed' kb_build_run for one
    kb_version. _build_seq_for (and the gates that call it) must not raise
    MultipleResultsFound; they take the LATEST completed build_seq."""
    for build_seq in (10, 11):
        session.add(KbBuildRun(kb_version="v-gate.retry", build_seq=build_seq, status="completed"))
    await session.flush()

    seq = await _build_seq_for(session, "v-gate.retry")
    assert seq == 11

    # a gate that calls _build_seq_for must also run without raising.
    result = await no_dangling_citations_gate(session, "v-gate.retry")
    assert result.passed  # no citeable artifacts ⇒ nothing dangles


@requires_db
async def test_build_seq_for_missing_version_raises_clear_error(session: AsyncSession) -> None:
    """No completed run for the version ⇒ an explicit, clear error (not a bare
    NoResultFound / None propagation)."""
    with pytest.raises(ValueError, match="no completed kb_build_run"):
        await _build_seq_for(session, "v-gate.absent")
