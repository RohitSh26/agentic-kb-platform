"""Persist one deterministic `commit` knowledge_artifact per git commit (PR-26).

Mirrors the artifact-writer shape but is zero-LLM: a commit produces exactly
one artifact (artifact_type='commit', body_text = the connector's normalized
rendering). No extraction, no graphify, no generation-cache row — the rendering is
fully deterministic from git, so re-running on an unchanged commit is gated by
the build runner's content_hash skip.

ACL propagation (docs/contracts/acl-source-visibility.md): a derived artifact is
visible only where EVERY input is, so the commit artifact's acl_teams is the
INTERSECTION of the acl_teams of the source_items for the files it changed. The
intersection rule itself (`commit_acl_intersection` / `DENY_ALL_ACL`) is pure
domain logic shared with the alias/reference miner (PR-38) and lives in
`domain.acl_intersection` — re-exported here for backward compatibility.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.domain.acl_intersection import (
    DENY_ALL_ACL,
    commit_acl_intersection,
    merge_path_acls,
)
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import KnowledgeArtifact, SourceItem
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

COMMIT_ARTIFACT_TYPE = "commit"
# A commit record is verbatim source-backed evidence (it is git's own data).
COMMIT_AUTHORITY = 1.0
BUILD_TIME_FRESHNESS = 1.0


async def _file_acls(
    session: AsyncSession, changed_files: Sequence[str], *, repo: str | None
) -> dict[str, list[str]]:
    """Map each changed file path to the acl_teams of its source_item, if any.

    Matches a code source_item by its path (repo-relative) WITHIN the commit's own
    `repo`. A same-path file in a DIFFERENT repo is a different file and must not
    contaminate this commit's ACL intersection (a phantom deny-all). A path with no
    matching source_item is absent from the map and contributes nothing to the
    intersection (it must not widen visibility).
    """
    if not changed_files:
        return {}
    rows = await session.execute(
        select(SourceItem.path, SourceItem.acl_teams).where(
            SourceItem.path.in_(list(changed_files)),
            SourceItem.repo == repo,
            SourceItem.is_deleted.is_(False),
        )
    )
    acl_rows_by_path: dict[str, list[list[str]]] = {}
    for path, acl_teams in rows.tuples():
        if path is None:
            continue
        acl_rows_by_path.setdefault(path, []).append(list(acl_teams))
    # Scoped to one repo, so several rows for one path are revisions of the SAME
    # file; the strictest wins (merge_path_acls) — a file is only org-public if
    # every such row is, and rows restricted to DISJOINT teams collapse to the
    # deny-all sentinel, never to [] (which would widen to everyone at read).
    return {path: merge_path_acls(acl_rows) for path, acl_rows in acl_rows_by_path.items()}


async def write_commit_artifact(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    kb_version: str,
    valid_from_seq: int = 0,
    title: str,
    body_text: str,
    changed_files: Sequence[str],
    repo: str | None,
) -> uuid.UUID:
    """Insert one `commit` knowledge_artifact and return its id.

    Flushes so the id is assigned but does not commit — the build runner owns
    the transaction. acl_teams is the intersection of the changed files' source
    ACLs (deny-by-default when nothing resolves), resolved WITHIN the commit's
    own `repo` so same-path files in other repos cannot contaminate it.
    """
    file_acls = await _file_acls(session, changed_files, repo=repo)
    acl_teams = commit_acl_intersection(changed_files, file_acls)
    artifact = KnowledgeArtifact(
        artifact_type=COMMIT_ARTIFACT_TYPE,
        source_id=source_id,
        title=title,
        body_text=body_text,
        content_hash=content_hash(body_text),
        kb_version=kb_version,
        valid_from_seq=valid_from_seq,
        knowledge_kind="source_backed",
        authority_score=COMMIT_AUTHORITY,
        freshness_score=BUILD_TIME_FRESHNESS,
        acl_teams=acl_teams,
    )
    session.add(artifact)
    await session.flush()
    logger.info(
        "event=commit_artifact_written source_id=%s kb_version=%s artifact_id=%s "
        "changed_files=%d resolved_inputs=%d acl_teams=%s",
        source_id,
        kb_version,
        artifact.artifact_id,
        len(changed_files),
        len(file_acls),
        acl_teams,
    )
    return artifact.artifact_id


__all__ = [
    "COMMIT_ARTIFACT_TYPE",
    "DENY_ALL_ACL",
    "commit_acl_intersection",
    "write_commit_artifact",
]
