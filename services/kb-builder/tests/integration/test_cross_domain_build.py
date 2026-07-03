"""Cross-domain deterministic links + commit ACL, DB-backed (PR-26).

Covers the brief's DB-level acceptance criteria:
- a commit artifact's acl_teams = INTERSECTION of its changed files' source ACLs
  (no leak, no widening), incl. unresolved files and the deny-by-default case.
- changed-file → code_file `mentions` edge created from commit metadata, with the
  changed-file path as the evidence pointer and relation_schema_version=1.
- commit → work-item `implements` edge from an explicit reference, evidence stored.
- idempotent re-link on unchanged inputs: a second run inserts/deletes nothing.

Skipped gracefully when TEST_DATABASE_URL is not configured (same policy as the
other DB-backed suites).
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application.write_commit import (
    COMMIT_ARTIFACT_TYPE,
    DENY_ALL_ACL,
    write_commit_artifact,
)
from agentic_kb_builder.connectors.git_metadata import CHANGED_FILES_HEADER
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)
from agentic_kb_builder.linker.run import run_linker

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
    repo: str | None = None,
    acl_teams: list[str] | None = None,
) -> SourceItem:
    source = SourceItem(
        source_type=source_type,
        source_uri=source_uri,
        source_version="1",
        path=path,
        external_id=external_id,
        branch=branch,
        repo=repo,
        content_hash=f"hash:{source_uri}",
        acl_teams=acl_teams or [],
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
    acl_teams: list[str] | None = None,
    kb_version: str = "v-build.1",
) -> KnowledgeArtifact:
    artifact = KnowledgeArtifact(
        artifact_type=artifact_type,
        source_id=source.source_id,
        title=title,
        body_text=body_text,
        kb_version=kb_version,
        acl_teams=acl_teams or [],
    )
    session.add(artifact)
    await session.flush()
    return artifact


def _commit_body(subject: str, files: list[str]) -> str:
    return "\n\n".join([subject, "\n".join([CHANGED_FILES_HEADER, *files])])


# --------------------------------------------------------------------------
# Commit-artifact ACL intersection (no leak / no widen)
# --------------------------------------------------------------------------


@requires_db
async def test_commit_acl_is_intersection_of_changed_file_sources(session: AsyncSession) -> None:
    # restricted.py is visible only to {payments}; public.py is org-public.
    await _add_source(
        session,
        source_type="github_code",
        source_uri="gh://repo/src/restricted.py",
        path="src/restricted.py",
        acl_teams=["payments"],
    )
    await _add_source(
        session,
        source_type="github_code",
        source_uri="gh://repo/src/public.py",
        path="src/public.py",
        acl_teams=[],
    )
    commit_source = await _add_source(
        session,
        source_type="git_metadata",
        source_uri="git:abc",
        external_id="a" * 40,
    )

    changed = ["src/public.py", "src/restricted.py"]
    artifact_id = await write_commit_artifact(
        session,
        source_id=commit_source.source_id,
        kb_version="v-build.1",
        title="abc",
        body_text=_commit_body("touch both", changed),
        changed_files=changed,
        repo=None,
    )

    acl = (
        await session.execute(
            select(KnowledgeArtifact.acl_teams).where(KnowledgeArtifact.artifact_id == artifact_id)
        )
    ).scalar_one()
    # the public file imposes no constraint; the restricted file gates the commit.
    # A team authorised only for "platform" can never see this commit (no leak),
    # and the org-public file never widened it to everyone (no widening).
    assert sorted(acl) == ["payments"]


@requires_db
async def test_commit_acl_unresolved_file_does_not_widen(session: AsyncSession) -> None:
    await _add_source(
        session,
        source_type="github_code",
        source_uri="gh://repo/src/secret.py",
        path="src/secret.py",
        acl_teams=["secret-team"],
    )
    commit_source = await _add_source(
        session, source_type="git_metadata", source_uri="git:def", external_id="d" * 40
    )
    changed = ["src/secret.py", "src/not_a_source.py"]
    artifact_id = await write_commit_artifact(
        session,
        source_id=commit_source.source_id,
        kb_version="v-build.1",
        title="def",
        body_text=_commit_body("touch", changed),
        changed_files=changed,
        repo=None,
    )
    acl = (
        await session.execute(
            select(KnowledgeArtifact.acl_teams).where(KnowledgeArtifact.artifact_id == artifact_id)
        )
    ).scalar_one()
    assert sorted(acl) == ["secret-team"]


@requires_db
async def test_commit_acl_deny_by_default_when_no_inputs_resolve(session: AsyncSession) -> None:
    commit_source = await _add_source(
        session, source_type="git_metadata", source_uri="git:ghi", external_id="g" * 40
    )
    artifact_id = await write_commit_artifact(
        session,
        source_id=commit_source.source_id,
        kb_version="v-build.1",
        title="ghi",
        body_text=_commit_body("touch", ["nowhere.py"]),
        changed_files=["nowhere.py"],
        repo=None,
    )
    acl = (
        await session.execute(
            select(KnowledgeArtifact.acl_teams).where(KnowledgeArtifact.artifact_id == artifact_id)
        )
    ).scalar_one()
    # Unknown provenance ⇒ deny by default. [] would mean org-public (everyone)
    # at read; the sentinel keeps the commit visible to nobody.
    assert list(acl) == list(DENY_ALL_ACL)


@requires_db
async def test_commit_acl_ignores_same_path_in_other_repo(session: AsyncSession) -> None:
    """KB-F4: a same-path file in a DIFFERENT repo must not narrow this commit's ACL.

    Repo A has an org-public file at src/shared.py; repo B has a restricted file at
    the same path. Building repo A's commit must stay org-public ([]). Without the
    repo filter, repo B's ["payments"] restriction would leak in and (intersected
    with org-public) wrongly narrow the commit to deny/payments — a phantom deny.
    """
    await _add_source(
        session,
        source_type="github_code",
        source_uri="gh://repoA/src/shared.py",
        path="src/shared.py",
        repo="repoA",
        acl_teams=[],
    )
    await _add_source(
        session,
        source_type="github_code",
        source_uri="gh://repoB/src/shared.py",
        path="src/shared.py",
        repo="repoB",
        acl_teams=["payments"],
    )
    commit_source = await _add_source(
        session,
        source_type="git_metadata",
        source_uri="git:jkl",
        external_id="j" * 40,
        repo="repoA",
    )
    changed = ["src/shared.py"]
    artifact_id = await write_commit_artifact(
        session,
        source_id=commit_source.source_id,
        kb_version="v-build.1",
        title="jkl",
        body_text=_commit_body("touch repoA", changed),
        changed_files=changed,
        repo="repoA",
    )
    acl = (
        await session.execute(
            select(KnowledgeArtifact.acl_teams).where(KnowledgeArtifact.artifact_id == artifact_id)
        )
    ).scalar_one()
    # repoA's only changed file is org-public; repoB's same-path restriction is a
    # different file and must not contaminate the intersection.
    assert list(acl) == []


@requires_db
async def test_commit_acl_denies_all_when_one_paths_rows_have_disjoint_teams(
    session: AsyncSession,
) -> None:
    """Two source rows for ONE (repo, path) restricted to DISJOINT teams: the
    per-path strictest-wins merge must collapse to the deny-all sentinel — the
    old empty-set merge stored [], which means org-public (everyone) at read."""
    await _add_source(
        session,
        source_type="github_code",
        source_uri="gh://repoA/src/dual.py",
        path="src/dual.py",
        repo="repoA",
        acl_teams=["team-a"],
    )
    await _add_source(
        session,
        source_type="github_doc",
        source_uri="gh://repoA/src/dual.py?doc",
        path="src/dual.py",
        repo="repoA",
        acl_teams=["team-b"],
    )
    commit_source = await _add_source(
        session,
        source_type="git_metadata",
        source_uri="git:mno",
        external_id="m" * 40,
        repo="repoA",
    )
    changed = ["src/dual.py"]
    artifact_id = await write_commit_artifact(
        session,
        source_id=commit_source.source_id,
        kb_version="v-build.1",
        title="mno",
        body_text=_commit_body("touch dual", changed),
        changed_files=changed,
        repo="repoA",
    )
    acl = (
        await session.execute(
            select(KnowledgeArtifact.acl_teams).where(KnowledgeArtifact.artifact_id == artifact_id)
        )
    ).scalar_one()
    assert list(acl) == list(DENY_ALL_ACL)


# --------------------------------------------------------------------------
# changed-file → code edge + commit→work-item implements, via run_linker
# --------------------------------------------------------------------------


async def _seed_cross_domain_chain(session: AsyncSession) -> dict[str, KnowledgeArtifact]:
    code_source = await _add_source(
        session,
        source_type="github_code",
        source_uri="gh://repo/src/service.py",
        path="src/service.py",
    )
    code_file = await _add_artifact(
        session,
        source=code_source,
        artifact_type="code_file",
        title="src/service.py",
        body_text="def f(): ...",
    )
    card_source = await _add_source(
        session,
        source_type="ado_card",
        source_uri="ado://card/4321",
        external_id="4321",
    )
    card = await _add_artifact(
        session,
        source=card_source,
        artifact_type="summary",
        title="Card 4321: ship service",
        body_text="Ship the service.",
    )
    commit_source = await _add_source(
        session,
        source_type="git_metadata",
        source_uri="git:c0ffee0",
        external_id="c0ffee0",
        branch="feature/AB-4321-svc",
    )
    commit = await _add_artifact(
        session,
        source=commit_source,
        artifact_type=COMMIT_ARTIFACT_TYPE,
        title="c0ffee0",
        body_text=_commit_body("AB#4321 add service", ["src/service.py"]),
    )
    return {"code_file": code_file, "card": card, "commit": commit}


@requires_db
async def test_cross_domain_edges_created_with_evidence(session: AsyncSession) -> None:
    chain = await _seed_cross_domain_chain(session)
    _inserted, _refreshed, deleted = await run_linker(session, kb_version="v-link.1")
    assert deleted == 0

    rows = await session.execute(
        select(
            KnowledgeEdge.from_artifact_id,
            KnowledgeEdge.to_artifact_id,
            KnowledgeEdge.edge_type,
            KnowledgeEdge.trust_class,
            KnowledgeEdge.relation_schema_version,
            KnowledgeEdge.evidence,
        ).where(KnowledgeEdge.source == "linker")
    )
    edges = {(frm, to, et): (trust, ver, ev) for frm, to, et, trust, ver, ev in rows.tuples()}

    commit = chain["commit"].artifact_id
    code_file = chain["code_file"].artifact_id
    card = chain["card"].artifact_id

    # changed-file → code_file mentions, evidence = the path, version 1, EXTRACTED.
    assert (commit, code_file, "mentions") in edges
    trust, ver, ev = edges[(commit, code_file, "mentions")]
    assert trust == "EXTRACTED"
    assert ver == 1
    assert ev == {"kind": "changed_file", "path": "src/service.py"}

    # commit → work-item implements, evidence = the matched reference.
    assert (commit, card, "implements") in edges
    trust, ver, ev = edges[(commit, card, "implements")]
    assert trust == "EXTRACTED"
    assert ver == 1
    assert ev == {"kind": "work_item_ref", "matched": "AB#4321"}


@requires_db
async def test_cross_domain_relink_is_idempotent(session: AsyncSession) -> None:
    await _seed_cross_domain_chain(session)
    first = await run_linker(session, kb_version="v-link.1")
    assert first[0] >= 2  # at least the implements + mentions inserted

    count_after_first = (
        await session.execute(select(KnowledgeEdge.edge_id).where(KnowledgeEdge.source == "linker"))
    ).all()

    # Re-run on unchanged inputs: zero churn (no inserts, no deletes).
    inserted, _refreshed, deleted = await run_linker(session, kb_version="v-link.1")
    assert inserted == 0
    assert deleted == 0
    count_after_second = (
        await session.execute(select(KnowledgeEdge.edge_id).where(KnowledgeEdge.source == "linker"))
    ).all()
    assert len(count_after_first) == len(count_after_second)


@requires_db
async def test_stale_cross_domain_implements_is_invalidated_without_a_provider(
    session: AsyncSession,
) -> None:
    # run_linker with similarity=None protects edge_type 'implements' from stale
    # invalidation (the skipped semantic pass would reproduce its symbol→concept
    # edges). A DETERMINISTIC cross-domain implements carries an evidence pointer,
    # so it is always recomputed when its reference still exists — its absence
    # means the reference is gone and it MUST be invalidated, never protected.
    # Invalidation is a soft-delete (invalidated_at_seq set), not row removal.
    #
    # The reference lives ONLY in the commit message here (no branch token), so
    # editing the message genuinely removes it.
    code_source = await _add_source(
        session,
        source_type="github_code",
        source_uri="gh://repo/src/widget.py",
        path="src/widget.py",
    )
    code_file = await _add_artifact(
        session,
        source=code_source,
        artifact_type="code_file",
        title="src/widget.py",
        body_text="def g(): ...",
    )
    card_source = await _add_source(
        session, source_type="ado_card", source_uri="ado://card/7777", external_id="7777"
    )
    card = await _add_artifact(
        session,
        source=card_source,
        artifact_type="summary",
        title="Card 7777",
        body_text="Build the widget.",
    )
    commit_source = await _add_source(
        session, source_type="git_metadata", source_uri="git:beadfee", external_id="beadfee"
    )
    commit = await _add_artifact(
        session,
        source=commit_source,
        artifact_type=COMMIT_ARTIFACT_TYPE,
        title="beadfee",
        body_text=_commit_body("AB#7777 add widget", ["src/widget.py"]),
    )
    await run_linker(session, kb_version="v-link.1")

    # The implements(commit→card) edge exists after the first run.
    first = (
        await session.execute(
            select(KnowledgeEdge.edge_id).where(
                KnowledgeEdge.source == "linker",
                KnowledgeEdge.edge_type == "implements",
                KnowledgeEdge.to_artifact_id == card.artifact_id,
            )
        )
    ).all()
    assert len(first) == 1

    # Remove the AB#7777 reference from the commit body: its evidence is gone, but
    # the changed-file mentions edge (still referenced) must survive.
    commit.body_text = _commit_body("add widget", ["src/widget.py"])
    await session.flush()

    _inserted, _refreshed, deleted = await run_linker(session, kb_version="v-link.1")
    assert deleted >= 1

    live_implements = (
        await session.execute(
            select(KnowledgeEdge.edge_id).where(
                KnowledgeEdge.source == "linker",
                KnowledgeEdge.edge_type == "implements",
                KnowledgeEdge.to_artifact_id == card.artifact_id,
                KnowledgeEdge.invalidated_at_seq.is_(None),
            )
        )
    ).all()
    assert live_implements == []  # stale deterministic implements no longer served

    # the row is not deleted — it is soft-invalidated (prior versions still serve it).
    invalidated_implements = (
        await session.execute(
            select(KnowledgeEdge.edge_id).where(
                KnowledgeEdge.source == "linker",
                KnowledgeEdge.edge_type == "implements",
                KnowledgeEdge.to_artifact_id == card.artifact_id,
                KnowledgeEdge.invalidated_at_seq.is_not(None),
            )
        )
    ).all()
    assert len(invalidated_implements) == 1

    surviving_mentions = (
        await session.execute(
            select(KnowledgeEdge.edge_id).where(
                KnowledgeEdge.source == "linker",
                KnowledgeEdge.edge_type == "mentions",
                KnowledgeEdge.from_artifact_id == commit.artifact_id,
                KnowledgeEdge.to_artifact_id == code_file.artifact_id,
                KnowledgeEdge.invalidated_at_seq.is_(None),
            )
        )
    ).all()
    assert len(surviving_mentions) == 1  # evidence still present ⇒ retained


@requires_db
async def test_no_false_implements_from_incidental_number_db(session: AsyncSession) -> None:
    card_source = await _add_source(
        session, source_type="ado_card", source_uri="ado://card/42", external_id="42"
    )
    card = await _add_artifact(
        session,
        source=card_source,
        artifact_type="summary",
        title="Card 42",
        body_text="A card.",
    )
    commit_source = await _add_source(
        session, source_type="git_metadata", source_uri="git:deadbee", external_id="deadbee"
    )
    await _add_artifact(
        session,
        source=commit_source,
        artifact_type=COMMIT_ARTIFACT_TYPE,
        title="deadbee",
        body_text=_commit_body("fixed 42 failing tests", []),
    )
    await run_linker(session, kb_version="v-link.1")
    implements_to_card = (
        await session.execute(
            select(KnowledgeEdge.edge_id).where(
                KnowledgeEdge.source == "linker",
                KnowledgeEdge.edge_type == "implements",
                KnowledgeEdge.to_artifact_id == card.artifact_id,
            )
        )
    ).all()
    assert implements_to_card == []
