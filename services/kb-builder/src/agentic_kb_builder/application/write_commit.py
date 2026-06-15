"""Persist one deterministic `commit` knowledge_artifact per git commit (PR-26).

Mirrors write_wikify_artifacts' shape but is zero-LLM: a commit produces exactly
one artifact (artifact_type='commit', body_text = the connector's normalized
rendering). No wikify, no graphify, no generation-cache row — the rendering is
fully deterministic from git, so re-running on an unchanged commit is gated by
the build runner's content_hash skip.

ACL propagation (docs/contracts/acl-source-visibility.md): a derived artifact is
visible only where EVERY input is, so the commit artifact's acl_teams is the
INTERSECTION of the acl_teams of the source_items for the files it changed.

Subtlety — empty acl_teams means org-public (everyone) at READ time (mcp-server
auth/rbac.py: `not artifact.acl_teams or requester.teams & artifact.acl_teams`).
So an org-public input imposes NO constraint on the intersection (it is the
universe of teams), and an EMPTY intersection result can NOT be stored as `[]` —
that would widen to everyone, the exact failure acl-source-visibility.md warns
against. We therefore store an explicit deny-all sentinel (`DENY_ALL_ACL`, a team
no requester holds) for "visible to nobody": disjoint restrictions (no common
team) and unknown provenance (zero resolvable inputs) both deny by default.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import KnowledgeArtifact, SourceItem
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

COMMIT_ARTIFACT_TYPE = "commit"
# A commit record is verbatim source-backed evidence (it is git's own data).
COMMIT_AUTHORITY = 1.0
BUILD_TIME_FRESHNESS = 1.0
# "Visible to nobody". Empty acl_teams means org-public (everyone) at read, so an
# empty intersection can't be stored as []; this sentinel — a team no real
# requester ever holds — denies all without a schema change. The broker's
# read-time edge-ACL intersection inherits it, so a denied commit's edges are
# hidden too. (Open question: a first-class deny needs a tri-state acl model.)
DENY_ALL_ACL: tuple[str, ...] = ("__no_team__",)


def commit_acl_intersection(
    changed_files: Sequence[str],
    file_acls: dict[str, list[str]],
) -> list[str]:
    """Visibility of a commit artifact = the teams authorised for EVERY changed file.

    A file with no source_item entry in `file_acls` contributes nothing (it
    cannot widen visibility). An org-public input (empty acl) is the universe of
    teams (rbac.py), so it imposes no constraint — only non-empty ACLs narrow.

    Results:
    - no constraining input (every resolved input org-public) ⇒ [] (org-public);
    - a non-empty intersection ⇒ that team set;
    - disjoint restrictions (constraints exist but share no team) ⇒ DENY_ALL_ACL;
    - zero resolvable inputs (unknown provenance) ⇒ DENY_ALL_ACL (deny by default).
    [] is NEVER used to mean "nobody" — it means "everyone" at read.
    """
    resolved = [file_acls[path] for path in changed_files if path in file_acls]
    if not resolved:
        # Unknown provenance: we can vouch for no team ⇒ deny by default.
        return list(DENY_ALL_ACL)
    # Org-public inputs (empty acl) impose no constraint; only non-empty ACLs
    # narrow. If every resolved input is org-public, the commit is org-public.
    constraints = [set(acl) for acl in resolved if acl]
    if not constraints:
        return []
    intersection = set.intersection(*constraints)
    if not intersection:
        # Disjoint teams: no team can see every input ⇒ visible to nobody.
        return list(DENY_ALL_ACL)
    return sorted(intersection)


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
    acls: dict[str, list[str]] = {}
    for path, acl_teams in rows.tuples():
        if path is None:
            continue
        # Scoped to one repo, so several rows for one path are revisions of the
        # SAME file; the strictest wins — a file is only org-public if every such
        # row is.
        existing = acls.get(path)
        teams = list(acl_teams)
        if existing is None:
            acls[path] = teams
        elif existing and teams:
            acls[path] = sorted(set(existing) & set(teams))
        else:
            # one side org-public ⇒ keep the constraining (non-empty) side, or
            # org-public if both empty.
            acls[path] = existing or teams
    return acls


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
