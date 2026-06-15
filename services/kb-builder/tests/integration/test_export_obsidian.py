"""DB-backed integration test for the Obsidian vault exporter (PR-obsidian).

Seeds a handful of artifacts of different types + edges between them against a
migrated DB, runs the exporter to a tmp_path, and asserts the vault layout:
one note per artifact in the right type folder, frontmatter fields present, the
"## Links" section wikilinks resolve to existing note slugs, and index.md exists.

Skipped gracefully when TEST_DATABASE_URL is not configured (same policy as the
other DB-backed suites).
"""

import os
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.export_obsidian import _resolve_target, export_obsidian
from agentic_kb_builder.infrastructure.postgres.models import (
    KbBuildRun,
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)

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

KB_VERSION = "v-obsidian.1"


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


async def _add_source(session: AsyncSession, *, source_type: str, source_uri: str) -> SourceItem:
    source = SourceItem(
        source_type=source_type,
        source_uri=source_uri,
        source_version="1",
        content_hash=f"hash:{source_uri}",
    )
    session.add(source)
    await session.flush()
    return source


async def _add_artifact(
    session: AsyncSession,
    *,
    source: SourceItem,
    artifact_type: str,
    title: str,
    body_text: str | None,
    acl_teams: list[str] | None = None,
) -> KnowledgeArtifact:
    artifact = KnowledgeArtifact(
        artifact_type=artifact_type,
        source_id=source.source_id,
        title=title,
        body_text=body_text,
        kb_version=KB_VERSION,
        acl_teams=acl_teams or [],
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def _add_edge(
    session: AsyncSession,
    *,
    frm: KnowledgeArtifact,
    to: KnowledgeArtifact,
    edge_type: str,
    trust_class: str = "EXTRACTED",
) -> None:
    session.add(
        KnowledgeEdge(
            from_artifact_id=frm.artifact_id,
            to_artifact_id=to.artifact_id,
            edge_type=edge_type,
            kb_version=KB_VERSION,
            trust_class=trust_class,
            source="linker",
        )
    )
    await session.flush()


async def _seed(session: AsyncSession) -> dict[str, KnowledgeArtifact]:
    code_source = await _add_source(
        session, source_type="github_code", source_uri="gh://repo/src/service.py"
    )
    code = await _add_artifact(
        session,
        source=code_source,
        artifact_type="code_file",
        title="src/service.py",
        body_text="def f(): ...",
        acl_teams=["payments"],
    )
    concept_source = await _add_source(
        session, source_type="github_doc", source_uri="gh://repo/docs/auth.md"
    )
    concept = await _add_artifact(
        session,
        source=concept_source,
        artifact_type="concept",
        title="Authentication: how it works",
        body_text="Auth is handled by the service.",
    )
    commit_source = await _add_source(session, source_type="git_metadata", source_uri="git:c0ffee0")
    commit = await _add_artifact(
        session,
        source=commit_source,
        artifact_type="commit",
        title="c0ffee0",
        body_text=None,  # exercises the placeholder body path
    )
    # commit --mentions--> code ; concept --describes--> code
    await _add_edge(session, frm=commit, to=code, edge_type="mentions")
    await _add_edge(session, frm=concept, to=code, edge_type="describes")
    await session.commit()
    return {"code": code, "concept": concept, "commit": commit}


@requires_db
async def test_export_writes_one_note_per_artifact_in_type_folders(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _seed(session)
    out = tmp_path / "vault"
    result = await export_obsidian(session, out=out, kb_version=KB_VERSION, build_seq=0)

    assert result.notes_written == 3
    code_note = out / "code" / "src-service-py.md"
    concept_note = out / "concepts" / "authentication-how-it-works.md"
    commit_note = out / "commits" / "c0ffee0.md"
    assert code_note.is_file()
    assert concept_note.is_file()
    assert commit_note.is_file()
    # index.md is the Map of Content.
    assert (out / "index.md").is_file()
    index = (out / "index.md").read_text(encoding="utf-8")
    assert "code" in index and "commit" in index and "concept" in index


@requires_db
async def test_frontmatter_fields_present(session: AsyncSession, tmp_path: Path) -> None:
    await _seed(session)
    out = tmp_path / "vault"
    await export_obsidian(session, out=out, kb_version=KB_VERSION, build_seq=0)

    code = (out / "code" / "src-service-py.md").read_text(encoding="utf-8")
    assert code.startswith("---\n")
    for field_name in (
        "id:",
        "type:",
        "title:",
        "kb_version:",
        "source_uri:",
        "acl_teams:",
        "trust:",
    ):
        assert field_name in code
    assert f'kb_version: "{KB_VERSION}"' in code
    assert 'type: "code_file"' in code
    assert 'source_uri: "gh://repo/src/service.py"' in code
    assert '"payments"' in code  # acl_teams rendered


@requires_db
async def test_links_section_wikilinks_resolve_to_existing_notes(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _seed(session)
    out = tmp_path / "vault"
    await export_obsidian(session, out=out, kb_version=KB_VERSION, build_seq=0)

    # The code note receives two incoming edges.
    code = (out / "code" / "src-service-py.md").read_text(encoding="utf-8")
    assert "## Links" in code
    assert "mentions [EXTRACTED]" in code
    assert "describes [EXTRACTED]" in code
    # Incoming links point back to the commit and concept notes by folder/slug.
    assert "[[commits/c0ffee0|c0ffee0]]" in code
    assert "[[concepts/authentication-how-it-works|Authentication: how it works]]" in code

    # The commit note's outgoing link must resolve to a file that exists.
    commit = (out / "commits" / "c0ffee0.md").read_text(encoding="utf-8")
    assert "mentions [EXTRACTED] → [[code/src-service-py|src/service.py]]" in commit
    for folder_slug in re.findall(r"\[\[([^|\]]+)\|", commit):
        assert (out / f"{folder_slug}.md").is_file()
    # Commit body was null ⇒ placeholder.
    assert "_(no body text)_" in commit


@requires_db
async def test_export_is_deterministic_and_idempotent(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _seed(session)
    out = tmp_path / "vault"
    await export_obsidian(session, out=out, kb_version=KB_VERSION, build_seq=0)
    first = {
        p.relative_to(out).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(out.rglob("*.md"))
    }
    # Re-run over the same KB into the same dir: byte-identical, no leftovers.
    await export_obsidian(session, out=out, kb_version=KB_VERSION, build_seq=0)
    second = {
        p.relative_to(out).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(out.rglob("*.md"))
    }
    assert first == second


@requires_db
async def test_resolve_target_active_and_unknown(session: AsyncSession) -> None:
    session.add(KbBuildRun(kb_version=KB_VERSION, build_seq=5, status="active"))
    await session.commit()
    # Default (None) resolves to the active run; an unknown label resolves to None.
    assert await _resolve_target(session, None) == (KB_VERSION, 5)
    assert await _resolve_target(session, "does-not-exist") is None


@requires_db
async def test_resolve_target_same_label_takes_highest_build_seq(session: AsyncSession) -> None:
    # A label is not unique (only build_seq is): a retried build can reuse it. The
    # explicit-label lookup must deterministically return the highest build_seq, the
    # only meaningful membership cutoff — not an arbitrary row.
    session.add(KbBuildRun(kb_version=KB_VERSION, build_seq=3, status="superseded"))
    session.add(KbBuildRun(kb_version=KB_VERSION, build_seq=8, status="active"))
    await session.commit()
    assert await _resolve_target(session, KB_VERSION) == (KB_VERSION, 8)


@requires_db
async def test_interval_membership_excludes_superseded_rows(
    session: AsyncSession, tmp_path: Path
) -> None:
    # A row invalidated at seq=2 is a member at S=1 but NOT at S=5 (ADR-0013). The
    # exporter must scope by the interval, not by a label, so a carried-over/retired
    # artifact appears in exactly the versions that own it.
    source = await _add_source(session, source_type="github_doc", source_uri="gh://repo/x.md")
    retired = await _add_artifact(
        session, source=source, artifact_type="concept", title="Retired Concept", body_text="x"
    )
    retired.invalidated_at_seq = 2  # left the KB at seq 2 (valid_from_seq defaults to 0)
    await session.commit()

    out = tmp_path / "vault"
    at_future = await export_obsidian(session, out=out, kb_version=KB_VERSION, build_seq=5)
    assert at_future.notes_written == 0  # excluded at S=5
    assert not (out / "concepts" / "retired-concept.md").exists()

    at_past = await export_obsidian(session, out=out, kb_version=KB_VERSION, build_seq=1)
    assert at_past.notes_written == 1  # included at S=1
    assert (out / "concepts" / "retired-concept.md").is_file()
