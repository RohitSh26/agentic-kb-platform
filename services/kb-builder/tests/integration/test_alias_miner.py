"""Alias/Reference index build pass (PR-38, docs/contracts/alias-reference.md).

Drives `run_alias_miner` directly against seeded `source_item` / commit
`knowledge_artifact` rows (the same style as `test_invalidation.py` /
`test_centrality_membership.py`): incremental skip on an unchanged source,
idempotent re-run (no duplicate rows/edges), and the never-widen ACL rule.

DB-backed; skipped without TEST_DATABASE_URL. Never run against the :55432 demo DB.
"""

import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.alias.resolve import resolve
from agentic_kb_builder.alias.run import (
    ALIAS_ARTIFACT_TYPE,
    ALIAS_EDGE_TYPE,
    _prior_extractions,
    run_alias_miner,
)
from agentic_kb_builder.alias.run import (
    load_alias_entries as load_alias_entries_from_db,
)
from agentic_kb_builder.application import EmbeddingResult
from agentic_kb_builder.application.build_runner import BuildRunner
from agentic_kb_builder.connectors.git_metadata import CHANGED_FILES_HEADER, GitMetadataConnector
from agentic_kb_builder.domain import DocExtractionResult, NormalizedContent, SourceRef, SourceType
from agentic_kb_builder.domain.acl_intersection import DENY_ALL_ACL
from agentic_kb_builder.domain.content_hasher import content_hash
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
_TABLES = (
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
        for tbl in _TABLES:
            await sess.execute(text(f"DELETE FROM {tbl}"))
        await sess.commit()
        yield sess
        await sess.rollback()
        for tbl in _TABLES:
            await sess.execute(text(f"DELETE FROM {tbl}"))
        await sess.commit()
    await engine.dispose()


async def _code_source(
    session: AsyncSession,
    path: str,
    *,
    acl_teams: tuple[str, ...] = (),
    repo: str = "o/r",
    uri_suffix: str = "",
) -> uuid.UUID:
    row = SourceItem(
        source_type="github_code",
        source_uri=f"repo://{repo}/{path}{uri_suffix}",
        source_version="rev-1",
        repo=repo,
        path=path,
        content_hash="code-hash-" + path,
        acl_teams=list(acl_teams),
        is_deleted=False,
    )
    session.add(row)
    await session.flush()
    return row.source_id


async def _code_artifact(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    path: str,
    artifact_type: str = "code_file",
) -> uuid.UUID:
    row = KnowledgeArtifact(
        artifact_type=artifact_type,
        source_id=source_id,
        title=path,
        body_text=None,
        kb_version="kb-v1",
        valid_from_seq=1,
        knowledge_kind="source_backed",
        acl_teams=[],
    )
    session.add(row)
    await session.flush()
    return row.artifact_id


def _commit_body(subject: str, changed_files: tuple[str, ...]) -> str:
    return "\n\n".join((subject, "\n".join((CHANGED_FILES_HEADER, *changed_files))))


async def _commit(
    session: AsyncSession, *, sha: str, subject: str, changed_files: tuple[str, ...]
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a git_metadata source_item + its `commit` artifact; returns (source_id, artifact_id)."""
    body = _commit_body(subject, changed_files)
    source = SourceItem(
        source_type="git_metadata",
        source_uri=f"git:{sha}",
        source_version=sha,
        repo="o/r",
        content_hash="commit-src-hash-" + sha,
        is_deleted=False,
    )
    session.add(source)
    await session.flush()
    artifact = KnowledgeArtifact(
        artifact_type="commit",
        source_id=source.source_id,
        title=sha[:12],
        body_text=body,
        content_hash=content_hash(body),
        kb_version="kb-v1",
        valid_from_seq=1,
        knowledge_kind="source_backed",
        acl_teams=[],
    )
    session.add(artifact)
    await session.flush()
    return source.source_id, artifact.artifact_id


async def _alias_rows(session: AsyncSession) -> list[KnowledgeArtifact]:
    rows = await session.execute(
        select(KnowledgeArtifact).where(KnowledgeArtifact.artifact_type == ALIAS_ARTIFACT_TYPE)
    )
    return list(rows.scalars().all())


async def _alias_edges(session: AsyncSession) -> list[KnowledgeEdge]:
    rows = await session.execute(
        select(KnowledgeEdge).where(KnowledgeEdge.edge_type == ALIAS_EDGE_TYPE)
    )
    return list(rows.scalars().all())


@requires_db
async def test_alias_miner_writes_artifacts_and_edges_from_a_commit(session: AsyncSession) -> None:
    code_source = await _code_source(session, "services/kb-builder/foo.py")
    code_artifact_id = await _code_artifact(
        session, source_id=code_source, path="services/kb-builder/foo.py"
    )
    await _commit(
        session,
        sha="a" * 40,
        subject="fix(alias): durable output cache",
        changed_files=("services/kb-builder/foo.py",),
    )

    result = await run_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    assert result.sources_seen == 1
    assert result.sources_mined == 1
    assert result.sources_skipped_unchanged == 0
    # phrases: scope "alias" + n-grams "durable output"/"output cache"/"durable output cache"
    assert result.artifacts_inserted == 4
    assert result.edges_inserted == 4

    rows = await _alias_rows(session)
    assert {r.title for r in rows} == {
        "alias",
        "durable output",
        "output cache",
        "durable output cache",
    }
    assert all(r.invalidated_at_seq is None for r in rows)
    assert all(r.acl_teams == [] for r in rows)  # org-public source ⇒ org-public alias

    edges = await _alias_edges(session)
    assert len(edges) == 4
    assert all(e.to_artifact_id == code_artifact_id for e in edges)
    assert all(e.source == "alias_miner" for e in edges)
    assert all(e.trust_class == "EXTRACTED" for e in edges)
    assert all(e.confidence == 1.0 for e in edges)

    entries = await load_alias_entries_from_db(session)
    resolution = resolve("durable output cache", entries)
    assert resolution is not None
    assert resolution.matched == "exact"
    assert resolution.targets == ("services/kb-builder/foo.py",)


@requires_db
async def test_alias_miner_incremental_skip_and_idempotent_rerun(session: AsyncSession) -> None:
    code_source = await _code_source(session, "services/kb-builder/foo.py")
    await _code_artifact(session, source_id=code_source, path="services/kb-builder/foo.py")
    await _commit(
        session,
        sha="b" * 40,
        subject="feat(kb-builder): release scrub pipeline",
        changed_files=("services/kb-builder/foo.py",),
    )

    first = await run_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()
    assert first.sources_mined == 1
    assert first.artifacts_inserted > 0
    artifact_count = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeArtifact)
            .where(KnowledgeArtifact.artifact_type == ALIAS_ARTIFACT_TYPE)
        )
    ).scalar_one()
    edge_count = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeEdge)
            .where(KnowledgeEdge.edge_type == ALIAS_EDGE_TYPE)
        )
    ).scalar_one()

    # Re-run over the SAME unchanged commit: the source is skipped (incremental),
    # and the reconcile writes/changes nothing (idempotent, no duplicates).
    second = await run_alias_miner(session, kb_version="kb-v2", valid_from_seq=2)
    await session.commit()

    assert second.sources_mined == 0
    assert second.sources_skipped_unchanged == 1
    assert second.artifacts_inserted == 0
    assert second.artifacts_refreshed == 0
    assert second.artifacts_unchanged == first.artifacts_inserted
    assert second.edges_inserted == 0
    assert second.edges_refreshed == 0
    assert second.edges_unchanged == first.edges_inserted

    artifact_count_after = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeArtifact)
            .where(KnowledgeArtifact.artifact_type == ALIAS_ARTIFACT_TYPE)
        )
    ).scalar_one()
    edge_count_after = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeEdge)
            .where(KnowledgeEdge.edge_type == ALIAS_EDGE_TYPE)
        )
    ).scalar_one()
    assert artifact_count_after == artifact_count
    assert edge_count_after == edge_count


@requires_db
async def test_alias_miner_remines_when_source_content_hash_changes(session: AsyncSession) -> None:
    code_source = await _code_source(session, "services/kb-builder/foo.py")
    await _code_artifact(session, source_id=code_source, path="services/kb-builder/foo.py")
    _, commit_artifact_id = await _commit(
        session,
        sha="c" * 40,
        subject="feat(kb-builder): embedding candidate floor",
        changed_files=("services/kb-builder/foo.py",),
    )

    first = await run_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()
    assert first.sources_mined == 1

    # simulate a changed commit artifact (new body_text ⇒ new content_hash): the
    # miner must treat it as CHANGED, not replay stale evidence.
    commit_artifact = await session.get(KnowledgeArtifact, commit_artifact_id)
    assert commit_artifact is not None
    new_body = _commit_body(
        "feat(kb-builder): embedding candidate ceiling", ("services/kb-builder/foo.py",)
    )
    commit_artifact.body_text = new_body
    commit_artifact.content_hash = content_hash(new_body)
    await session.flush()

    second = await run_alias_miner(session, kb_version="kb-v2", valid_from_seq=2)
    await session.commit()
    assert second.sources_mined == 1
    assert second.sources_skipped_unchanged == 0


@requires_db
async def test_alias_acl_is_never_widened_by_an_org_public_co_target(session: AsyncSession) -> None:
    public_source = await _code_source(session, "services/kb-builder/public.py", acl_teams=())
    restricted_source = await _code_source(
        session, "services/kb-builder/secret.py", acl_teams=("security",)
    )
    await _code_artifact(session, source_id=public_source, path="services/kb-builder/public.py")
    await _code_artifact(session, source_id=restricted_source, path="services/kb-builder/secret.py")
    await _commit(
        session,
        sha="d" * 40,
        subject="fix(alias): resolve relative imports",
        changed_files=("services/kb-builder/public.py", "services/kb-builder/secret.py"),
    )

    await run_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    rows = await _alias_rows(session)
    assert rows
    # visibility must be the intersection (the security-restricted target), NEVER
    # widened to org-public just because one co-target was public.
    assert all(r.acl_teams == ["security"] for r in rows)


@requires_db
async def test_alias_acl_denies_all_on_disjoint_restricted_targets(session: AsyncSession) -> None:
    security_source = await _code_source(
        session, "services/kb-builder/sec.py", acl_teams=("security",)
    )
    finance_source = await _code_source(
        session, "services/kb-builder/fin.py", acl_teams=("finance",)
    )
    await _code_artifact(session, source_id=security_source, path="services/kb-builder/sec.py")
    await _code_artifact(session, source_id=finance_source, path="services/kb-builder/fin.py")
    await _commit(
        session,
        sha="e" * 40,
        subject="fix(alias): ado wiki path filtering",
        changed_files=("services/kb-builder/sec.py", "services/kb-builder/fin.py"),
    )

    await run_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    rows = await _alias_rows(session)
    assert rows
    assert all(r.acl_teams == list(DENY_ALL_ACL) for r in rows)


@requires_db
async def test_alias_target_resolution_is_scoped_to_the_contributing_repo(
    session: AsyncSession,
) -> None:
    """Same path in TWO repos: the alias mined from an o/r commit must bind to
    o/r's artifact with o/r's ACL. Unscoped resolution would prefer the OTHER
    repo's code_file artifact (wrong repo) and contaminate the ACL with its
    restricted teams (alias-reference.md: resolution is (repo, path))."""
    path = "services/kb-builder/shared.py"
    # o/r: org-public, only a summary artifact (so the wrong-repo code_file would
    # deterministically win under unscoped type preference)
    own_source = await _code_source(session, path, repo="o/r", acl_teams=())
    own_artifact = await _code_artifact(
        session, source_id=own_source, path=path, artifact_type="summary"
    )
    # unrelated repo: restricted, with a code_file artifact at the SAME path
    other_source = await _code_source(session, path, repo="other/r", acl_teams=("team-b",))
    await _code_artifact(session, source_id=other_source, path=path, artifact_type="code_file")
    await _commit(
        session,
        sha="f" * 40,
        subject="fix(alias): shared module cleanup",
        changed_files=(path,),
    )

    await run_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    rows = await _alias_rows(session)
    assert rows
    # o/r's file is org-public; the other repo's ["team-b"] must not leak in
    assert all(r.acl_teams == [] for r in rows)
    edges = await _alias_edges(session)
    assert edges
    assert all(e.to_artifact_id == own_artifact for e in edges)


@requires_db
async def test_alias_acl_denies_all_when_one_paths_rows_have_disjoint_teams(
    session: AsyncSession,
) -> None:
    """Two source rows for ONE path restricted to DISJOINT teams: the per-path
    merge must collapse to the deny-all sentinel — storing [] would mean
    org-public at read (rbac treats [] as everyone), the never-widen failure."""
    path = "services/kb-builder/dual.py"
    source_a = await _code_source(session, path, acl_teams=("team-a",))
    await _code_source(session, path, acl_teams=("team-b",), uri_suffix="?rev=2")
    await _code_artifact(session, source_id=source_a, path=path)
    await _commit(
        session,
        sha="9" * 40,
        subject="fix(alias): dual ownership handling",
        changed_files=(path,),
    )

    await run_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    rows = await _alias_rows(session)
    assert rows
    assert all(r.acl_teams == list(DENY_ALL_ACL) for r in rows)


# ---------------------------------------------------------------------------
# Watermark reconstruction (_prior_extractions) — pure, no DB
# ---------------------------------------------------------------------------


def _stored_alias_row(source: str, mined_hash: str, targets: tuple[str, ...]) -> KnowledgeArtifact:
    body = {
        "schema": "alias_reference_v1",
        "evidence": [
            {"source": source, "ref": "r", "content_hash": mined_hash, "targets": list(targets)}
        ],
    }
    return KnowledgeArtifact(artifact_type=ALIAS_ARTIFACT_TYPE, body_text=json.dumps(body))


def test_watermark_conflict_poisons_the_source_for_the_whole_scan() -> None:
    """Hash pattern A,B,A across one source's stored rows: after the B conflict a
    LATER matching-hash row must NOT re-add a PARTIAL phrase map (pop-then-readd
    would replay only the post-conflict phrases and invalidate the rest as
    no_contributing_source on an unchanged source). The source must re-mine."""
    prior_rows = {
        "phrase a": _stored_alias_row("commit:git:s1", "hash-A", ("a.py",)),
        "phrase b": _stored_alias_row("commit:git:s1", "hash-B", ("b.py",)),
        "phrase c": _stored_alias_row("commit:git:s1", "hash-A", ("c.py",)),
    }

    assert "commit:git:s1" not in _prior_extractions(prior_rows)


def test_watermark_agreeing_hashes_reconstruct_the_full_phrase_map() -> None:
    prior_rows = {
        "phrase a": _stored_alias_row("commit:git:s2", "hash-A", ("a.py",)),
        "phrase b": _stored_alias_row("commit:git:s2", "hash-A", ("b.py",)),
    }

    state = _prior_extractions(prior_rows)
    assert state["commit:git:s2"] == (
        "hash-A",
        {"phrase a": ("a.py",), "phrase b": ("b.py",)},
    )


# ---------------------------------------------------------------------------
# End-to-end wiring: BuildRunner._finalize_graph actually calls run_alias_miner
# (the real git_metadata connector against a throwaway repo, no fakes for the
# mining path itself — only doc_extractor/embedder/indexer are faked).
# ---------------------------------------------------------------------------


class _UnusedDocExtractor:
    """git_metadata is zero-LLM (connectors.md); this must never be called."""

    model_name = "unused"
    model_params_hash = "unused"

    async def extract(self, content: NormalizedContent) -> DocExtractionResult:
        raise AssertionError("docify must not run for a git_metadata-only build")


class _FakeEmbedder:
    embedding_model = "embed-test"

    async def embed(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(embedding_hash="emb-" + content_hash(text)[:12], vector=[0.1, 0.2])


class _FakeIndexer:
    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int:
        return len(artifact_ids)

    async def delete_orphaned(self) -> int:
        return 0

    async def reconcile_missing(self) -> int:
        return 0


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.test")
    _git(root, "config", "user.name", "Tester")
    (root / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fix(alias-wiring): resolve relative imports")
    return root


@requires_db
async def test_build_runner_writes_alias_rows_via_the_real_finalize_graph_pass(
    session: AsyncSession, git_repo: Path
) -> None:
    """Proves the wiring, not just the pass in isolation: a real `BuildRunner.run()`
    over a `GitMetadataConnector` must produce `alias_reference` artifacts — the
    acceptance criterion is "written by the build", not "callable directly"."""
    runner = BuildRunner(
        session,
        kb_version="kb-wiring-test",
        doc_extractor=_UnusedDocExtractor(),
        embedder=_FakeEmbedder(),
        indexer=_FakeIndexer(),
    )
    run = await runner.run([GitMetadataConnector(git_repo)])
    await session.commit()

    assert run.status == "completed"
    rows = await _alias_rows(session)
    assert rows
    assert any(r.title == "resolve relative imports" for r in rows)


class _FakeGithubCodeConnector:
    """One github_code source, one file, fully in-memory (no LocalFsBackend — that
    backend keys source_item identity on the file's `file://` URI alone, so it
    cannot represent the SAME path existing in two DIFFERENT repos, exactly the
    mixed-repo scenario this test needs)."""

    source_type: ClassVar[SourceType] = "github_code"

    def __init__(self, *, repo: str, path: str, text: str, acl_teams: tuple[str, ...]) -> None:
        self._repo = repo
        self._path = path
        self._text = text
        self._acl_teams = acl_teams

    async def list_sources(self) -> list[SourceRef]:
        return [
            SourceRef(
                source_type="github_code",
                source_uri=f"repo://{self._repo}/{self._path}",
                source_version="rev-1",
                repo=self._repo,
                path=self._path,
                acl_teams=list(self._acl_teams),
            )
        ]

    async def fetch(self, source: SourceRef) -> NormalizedContent:
        return NormalizedContent(
            source=source, text=self._text, content_hash=content_hash(self._text)
        )


@requires_db
async def test_build_runner_binds_commit_mined_alias_to_the_correct_repo_in_a_mixed_workspace(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Fix: git_metadata repo provenance. In ONE build with TWO github_code repos that
    both own a file at the SAME path, a real `GitMetadataConnector(repo="o/r")` commit
    touching that path must bind its alias — and the commit artifact's own ACL — ONLY
    to o/r's artifact/source, never other/r's (restricted) same-path one. Proves the
    connector's stamped `repo` flows through `write_commit_artifact` and
    `run_alias_miner`'s (repo, path) scoping end to end via the REAL build pipeline —
    not just via directly-seeded rows (`test_alias_target_resolution_is_scoped_to_the_
    contributing_repo`, which does not exercise `GitMetadataConnector` at all). Before
    this fix (repo always None), this resolved NOTHING (safe, but under-resolving); an
    unscoped fix would instead leak other/r's restricted team (the KB-F4 precedent)."""
    path = "shared.py"
    text = "def shared():\n    return 1\n"

    root = tmp_path / "mixed-repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.test")
    _git(root, "config", "user.name", "Tester")
    (root / path).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fix(alias-repo): shared module cleanup")

    runner = BuildRunner(
        session,
        kb_version="kb-mixed-repo",
        doc_extractor=_UnusedDocExtractor(),
        embedder=_FakeEmbedder(),
        indexer=_FakeIndexer(),
    )
    run = await runner.run(
        [
            _FakeGithubCodeConnector(repo="o/r", path=path, text=text, acl_teams=()),
            _FakeGithubCodeConnector(repo="other/r", path=path, text=text, acl_teams=("team-b",)),
            GitMetadataConnector(root, repo="o/r"),
        ]
    )
    await session.commit()
    assert run.status == "completed"

    own_source_id = (
        await session.execute(
            select(SourceItem.source_id).where(SourceItem.repo == "o/r", SourceItem.path == path)
        )
    ).scalar_one()
    other_source_id = (
        await session.execute(
            select(SourceItem.source_id).where(
                SourceItem.repo == "other/r", SourceItem.path == path
            )
        )
    ).scalar_one()
    own_artifact_ids = {
        row
        for row in (
            await session.execute(
                select(KnowledgeArtifact.artifact_id).where(
                    KnowledgeArtifact.source_id == own_source_id
                )
            )
        ).scalars()
    }
    other_artifact_ids = {
        row
        for row in (
            await session.execute(
                select(KnowledgeArtifact.artifact_id).where(
                    KnowledgeArtifact.source_id == other_source_id
                )
            )
        ).scalars()
    }

    edges = await _alias_edges(session)
    assert edges
    to_ids = {edge.to_artifact_id for edge in edges}
    assert to_ids & own_artifact_ids
    assert not (to_ids & other_artifact_ids)
    rows = await _alias_rows(session)
    assert rows
    # o/r's file is org-public; other/r's ["team-b"] must never leak in.
    assert all(row.acl_teams == [] for row in rows)

    commit_rows = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(KnowledgeArtifact.artifact_type == "commit")
            )
        )
        .scalars()
        .all()
    )
    assert commit_rows
    assert all(row.acl_teams == [] for row in commit_rows)
