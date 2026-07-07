"""Ledger-mined alias corrections (PR-43, ADR-0034, docs/contracts/alias-reference.md
"Ledger-mined aliases"). Drives `run_ledger_alias_miner` directly against seeded
`retrieval_event` misses + live candidate artifacts (same style as
`test_alias_miner.py`): matched vs unresolved misses, ACL never-widened, length
cap + untrusted-content normalization, idempotent re-run, title-collision safety
against PR-38's OWN alias miner, and the golden-set-unaffected proof.

DB-backed; skipped without TEST_DATABASE_URL. Never run against the :55432 demo DB.
"""

import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.alias.ledger_mining import (
    MAX_RAW_QUERY_CHARS,
    _sanitize_query_text,
    run_ledger_alias_miner,
)
from agentic_kb_builder.alias.resolve import resolve
from agentic_kb_builder.alias.run import (
    ALIAS_ARTIFACT_TYPE,
    LEDGER_MINED_PROVENANCE,
    load_alias_entries,
    run_alias_miner,
)
from agentic_kb_builder.application import EmbeddingResult
from agentic_kb_builder.application.build_runner import BuildRunner
from agentic_kb_builder.connectors.git_metadata import CHANGED_FILES_HEADER, GitMetadataConnector
from agentic_kb_builder.domain import DocExtractionResult, NormalizedContent
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    RetrievalEvent,
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
) -> uuid.UUID:
    row = SourceItem(
        source_type="github_code",
        source_uri=f"repo://{repo}/{path}",
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
    session: AsyncSession, *, source_id: uuid.UUID, title: str, artifact_type: str = "code_file"
) -> uuid.UUID:
    row = KnowledgeArtifact(
        artifact_type=artifact_type,
        source_id=source_id,
        title=title,
        body_text=None,
        kb_version="kb-v1",
        valid_from_seq=1,
        knowledge_kind="source_backed",
        acl_teams=[],
    )
    session.add(row)
    await session.flush()
    return row.artifact_id


async def _miss(
    session: AsyncSession,
    query_text: str,
    *,
    created_at: datetime | None = None,
    tool_name: str = "kb_search",
    status: str = "approved",
    returned: list[uuid.UUID] | None = None,
) -> None:
    row = RetrievalEvent(
        run_id="-",
        agent_name="implementation",
        tool_name=tool_name,
        status=status,
        query_text=query_text,
        kb_version="kb-v1",
        returned_artifact_ids=returned,
    )
    session.add(row)
    await session.flush()
    if created_at is not None:
        row.created_at = created_at
        await session.flush()


async def _alias_rows(session: AsyncSession) -> list[KnowledgeArtifact]:
    rows = await session.execute(
        select(KnowledgeArtifact).where(KnowledgeArtifact.artifact_type == ALIAS_ARTIFACT_TYPE)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Untrusted-content handling — pure, no DB
# ---------------------------------------------------------------------------


def test_sanitize_strips_control_chars_and_caps_length() -> None:
    raw = "the\x00 durable\x1f cache\x7f fix" + "x" * 200
    sanitized = _sanitize_query_text(raw)
    assert "\x00" not in sanitized and "\x1f" not in sanitized and "\x7f" not in sanitized
    assert len(sanitized) <= MAX_RAW_QUERY_CHARS


# ---------------------------------------------------------------------------
# Matched vs unresolved misses
# ---------------------------------------------------------------------------


@requires_db
async def test_ledger_mining_creates_alias_for_a_matched_miss(session: AsyncSession) -> None:
    source = await _code_source(session, "services/kb-builder/durable_output_cache.py")
    artifact_id = await _code_artifact(session, source_id=source, title="durable output cache")
    await _miss(session, "the durable cache fix")
    await _miss(session, "the durable cache fix")  # second miss, same phrase, same day

    result = await run_ledger_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    assert result.misses_seen == 2
    assert result.phrases_seen == 1
    assert result.mined == 1
    assert result.unresolved == 0
    assert result.artifacts_inserted == 1

    rows = await _alias_rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.title == "durable cache fix"
    assert row.acl_teams == []  # org-public source => org-public alias
    assert row.invalidated_at_seq is None
    body = json.loads(row.body_text or "{}")
    assert body["provenance"] == LEDGER_MINED_PROVENANCE
    assert body["targets"][0]["path"] == "services/kb-builder/durable_output_cache.py"
    assert body["targets"][0]["artifact_id"] == str(artifact_id)
    assert body["confirmation_count"] == 1  # both misses on the SAME UTC day
    assert len(body["evidence"]) == 1
    assert body["evidence"][0]["miss_count"] == 2


@requires_db
async def test_unmatched_miss_stays_unresolved(session: AsyncSession) -> None:
    await _code_source(session, "services/kb-builder/unrelated.py")
    await _miss(session, "totally unrelated gibberish phrase")

    result = await run_ledger_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    assert result.mined == 0
    assert result.unresolved == 1
    rows = await _alias_rows(session)
    assert rows == []


@requires_db
async def test_thin_and_denied_kb_search_rows_are_scoped_correctly(session: AsyncSession) -> None:
    """Only tool_name='kb_search' AND status='approved' AND <=1 returned ids counts
    as a miss — the SAME predicate migration 0020's kb_search_zero_thin uses."""
    source = await _code_source(session, "services/kb-builder/widget.py")
    await _code_artifact(session, source_id=source, title="widget module")
    healthy_id = uuid.uuid4()
    await _miss(session, "widget module", status="denied")  # budget denial, not a gap
    await _miss(session, "widget module", tool_name="context.create_pack")  # wrong tool
    await _miss(session, "widget module", returned=[healthy_id, uuid.uuid4()])  # answered well

    result = await run_ledger_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    assert result.misses_seen == 0
    assert result.phrases_seen == 0


@requires_db
async def test_window_days_excludes_older_misses(session: AsyncSession) -> None:
    source = await _code_source(session, "services/kb-builder/aged.py")
    await _code_artifact(session, source_id=source, title="aged module")
    await _miss(session, "aged module", created_at=datetime.now(UTC) - timedelta(days=30))
    await _miss(session, "aged module", created_at=datetime.now(UTC) - timedelta(days=1))

    result = await run_ledger_alias_miner(
        session, kb_version="kb-v1", valid_from_seq=1, window_days=14
    )
    await session.commit()

    assert result.misses_seen == 1  # only the 1-day-old miss is inside the window
    assert result.mined == 1


# ---------------------------------------------------------------------------
# ACL never-widened
# ---------------------------------------------------------------------------


@requires_db
async def test_ledger_alias_inherits_the_restricted_target_acl(session: AsyncSession) -> None:
    source = await _code_source(session, "services/kb-builder/secret.py", acl_teams=("security",))
    await _code_artifact(session, source_id=source, title="secret module")
    await _miss(session, "secret module")

    await run_ledger_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    rows = await _alias_rows(session)
    assert rows
    assert rows[0].acl_teams == ["security"]


# ---------------------------------------------------------------------------
# Idempotent re-run
# ---------------------------------------------------------------------------


@requires_db
async def test_idempotent_rerun_creates_no_duplicates(session: AsyncSession) -> None:
    source = await _code_source(session, "services/kb-builder/stable.py")
    await _code_artifact(session, source_id=source, title="stable module")
    await _miss(session, "stable module")

    first = await run_ledger_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()
    assert first.artifacts_inserted == 1

    second = await run_ledger_alias_miner(session, kb_version="kb-v2", valid_from_seq=2)
    await session.commit()

    assert second.artifacts_inserted == 0
    assert second.artifacts_refreshed == 0
    assert second.artifacts_unchanged == 1
    assert second.artifacts_invalidated == 0
    rows = await _alias_rows(session)
    assert len(rows) == 1


@requires_db
async def test_a_new_miss_for_the_same_phrase_refreshes_not_duplicates(
    session: AsyncSession,
) -> None:
    source = await _code_source(session, "services/kb-builder/growing.py")
    await _code_artifact(session, source_id=source, title="growing module")
    await _miss(session, "growing module")
    first = await run_ledger_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()
    assert first.artifacts_inserted == 1

    # a SECOND day's miss for the same phrase => confirmation_count grows to 2
    await _miss(session, "growing module", created_at=datetime.now(UTC) - timedelta(days=1))
    second = await run_ledger_alias_miner(session, kb_version="kb-v2", valid_from_seq=2)
    await session.commit()

    assert second.artifacts_inserted == 0
    assert second.artifacts_refreshed == 1
    rows = await _alias_rows(session)
    assert len(rows) == 1
    body = json.loads(rows[0].body_text or "{}")
    assert body["confirmation_count"] == 2


@requires_db
async def test_a_miss_that_falls_out_of_the_window_gets_invalidated(
    session: AsyncSession,
) -> None:
    source = await _code_source(session, "services/kb-builder/fading.py")
    await _code_artifact(session, source_id=source, title="fading module")
    await _miss(session, "fading module", created_at=datetime.now(UTC) - timedelta(days=10))
    first = await run_ledger_alias_miner(
        session, kb_version="kb-v1", valid_from_seq=1, window_days=14
    )
    await session.commit()
    assert first.artifacts_inserted == 1

    # a NARROWER window on the next build no longer sees that (now-old) miss.
    second = await run_ledger_alias_miner(
        session, kb_version="kb-v2", valid_from_seq=2, window_days=7
    )
    await session.commit()

    assert second.artifacts_invalidated == 1
    rows = await _alias_rows(session)
    assert rows[0].invalidated_at_seq == 2


# ---------------------------------------------------------------------------
# Title-collision safety against PR-38's OWN alias miner
# ---------------------------------------------------------------------------


@requires_db
async def test_ledger_mining_never_duplicates_a_title_already_owned_by_build_mining(
    session: AsyncSession,
) -> None:
    path = "services/kb-builder/foo.py"
    code_source = await _code_source(session, path)
    # Ledger-mining's OWN candidate matching must independently resolve this
    # phrase too (a real, differently-titled candidate) — otherwise resolve()
    # never even reaches the title-collision check below.
    await _code_artifact(session, source_id=code_source, title="durable output cache")
    commit_source = SourceItem(
        source_type="git_metadata",
        source_uri="git:" + "a" * 40,
        source_version="a" * 40,
        repo="o/r",
        content_hash="commit-hash",
        is_deleted=False,
    )
    session.add(commit_source)
    await session.flush()
    body = "\n\n".join(
        ("fix(alias): durable output cache", "\n".join((CHANGED_FILES_HEADER, path)))
    )
    session.add(
        KnowledgeArtifact(
            artifact_type="commit",
            source_id=commit_source.source_id,
            title="a" * 12,
            body_text=body,
            content_hash=content_hash(body),
            kb_version="kb-v1",
            valid_from_seq=1,
            knowledge_kind="source_backed",
            acl_teams=[],
        )
    )
    await session.flush()

    # A developer misses on the EXACT phrase PR-38's alias miner will mine from
    # this commit's subject n-grams.
    await _miss(session, "durable output cache")

    await run_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    ledger_result = await run_ledger_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    assert ledger_result.already_aliased == 1
    assert ledger_result.artifacts_inserted == 0
    rows = await _alias_rows(session)
    matching = [r for r in rows if r.title == "durable output cache"]
    assert len(matching) == 1  # one row, never duplicated
    assert json.loads(matching[0].body_text or "{}").get("provenance") != LEDGER_MINED_PROVENANCE


@requires_db
async def test_build_alias_miner_never_invalidates_a_ledger_mined_row(
    session: AsyncSession,
) -> None:
    """Regression test for the alias/run.py invalidation-sweep fix: without the
    provenance skip, run_alias_miner would invalidate EVERY ledger-mined row on
    every build (it never desires a ledger-mined title)."""
    source = await _code_source(session, "services/kb-builder/lonely.py")
    await _code_artifact(session, source_id=source, title="lonely module")
    await _miss(session, "lonely module")
    await run_ledger_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()
    rows = await _alias_rows(session)
    assert len(rows) == 1 and rows[0].invalidated_at_seq is None

    # A build with NO commits/docs at all: run_alias_miner desires NOTHING.
    result = await run_alias_miner(session, kb_version="kb-v2", valid_from_seq=2)
    await session.commit()

    assert result.artifacts_invalidated == 0  # the ledger-mined row must be untouched
    rows = await _alias_rows(session)
    assert len(rows) == 1 and rows[0].invalidated_at_seq is None


# ---------------------------------------------------------------------------
# Golden-set-unaffected proof (ADR-0034 success metric, "proved in-suite")
# ---------------------------------------------------------------------------


@requires_db
async def test_golden_alias_resolution_is_unaffected_by_ledger_mining(
    session: AsyncSession,
) -> None:
    """A representative slice of the alias golden set
    (evals/retrieval_cases/alias_golden_v1.yaml alias-01 / alias-16) must resolve
    identically whether or not the ledger-mining pass has ALSO run this build —
    the Goodhart line (ADR-0034): mining creates candidates from real usage, it
    never tunes against — or degrades — the golden set."""
    durable_source = await _code_source(
        session, "services/kb-builder/infrastructure/postgres/durable_output_cache.py"
    )
    await _code_artifact(
        session,
        source_id=durable_source,
        title="services/kb-builder/infrastructure/postgres/durable_output_cache.py",
    )
    commit_source = SourceItem(
        source_type="git_metadata",
        source_uri="git:" + "b" * 40,
        source_version="b" * 40,
        repo="o/r",
        content_hash="commit-hash-b",
        is_deleted=False,
    )
    session.add(commit_source)
    await session.flush()
    changed = "services/kb-builder/infrastructure/postgres/durable_output_cache.py"
    body = "\n\n".join(
        (
            "fix(kb-builder): durable model-output cache is fail-soft, never crashes the build",
            "\n".join((CHANGED_FILES_HEADER, changed)),
        )
    )
    session.add(
        KnowledgeArtifact(
            artifact_type="commit",
            source_id=commit_source.source_id,
            title="b" * 12,
            body_text=body,
            content_hash=content_hash(body),
            kb_version="kb-v1",
            valid_from_seq=1,
            knowledge_kind="source_backed",
            acl_teams=[],
        )
    )
    await session.flush()

    # unrelated ledger traffic, WITH a real target — must produce its OWN
    # ledger-mined alias without ever interfering with the golden phrase above.
    budget_source = await _code_source(session, "services/mcp-server/budgets.py")
    await _code_artifact(session, source_id=budget_source, title="retrieval budget check")
    await _miss(session, "the retrieval budget check")

    await run_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    ledger_result = await run_ledger_alias_miner(session, kb_version="kb-v1", valid_from_seq=1)
    await session.commit()

    assert ledger_result.artifacts_inserted == 1  # the unrelated phrase DID mine

    entries = await load_alias_entries(session)
    result = resolve("the durable cache fail soft fix", entries)
    assert result is not None
    assert result.matched == "fuzzy"
    assert result.targets[0] == (
        "services/kb-builder/infrastructure/postgres/durable_output_cache.py"
    )


# ---------------------------------------------------------------------------
# End-to-end wiring: BuildRunner._finalize_graph actually calls
# run_ledger_alias_miner AFTER run_alias_miner, and persists its counters onto
# kb_build_run (migration 0022).
# ---------------------------------------------------------------------------


class _UnusedDocExtractor:
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
    (root / "widget.py").write_text("def widget():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fix(widget): stabilize widget module")
    return root


@requires_db
async def test_build_runner_wires_ledger_mining_after_alias_mining(
    session: AsyncSession, git_repo: Path
) -> None:
    """Proves the wiring, not just the pass in isolation: a real `BuildRunner.run()`
    with a seeded miss must produce a ledger-mined alias AND persist the
    ledger-mining counters onto kb_build_run — "written by the build". The miss
    query is the file's own name so it resolves EXACTLY against the code_file
    candidate's title=path (deterministic — no dependence on AST symbol-naming).
    A live code candidate is seeded directly (this build only runs
    GitMetadataConnector — zero-LLM, no github_code connector — so nothing else
    would create one)."""
    code_source = await _code_source(session, "widget.py")
    await _code_artifact(session, source_id=code_source, title="widget.py")
    await _miss(session, "widget.py")

    runner = BuildRunner(
        session,
        kb_version="kb-ledger-wiring-test",
        doc_extractor=_UnusedDocExtractor(),
        embedder=_FakeEmbedder(),
        indexer=_FakeIndexer(),
    )
    run = await runner.run([GitMetadataConnector(git_repo)])
    await session.commit()

    assert run.status == "completed"
    assert run.ledger_mining_misses_seen == 1
    assert run.ledger_mining_mined + run.ledger_mining_unresolved == 1

    rows = await _alias_rows(session)
    ledger_rows = [
        r
        for r in rows
        if json.loads(r.body_text or "{}").get("provenance") == LEDGER_MINED_PROVENANCE
    ]
    assert ledger_rows  # the seeded miss resolved against the commit-mined widget alias
