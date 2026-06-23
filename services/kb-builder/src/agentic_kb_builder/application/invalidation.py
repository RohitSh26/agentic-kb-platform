"""Identity-over-time invalidation pass (PR-27, ADR-0013).

Runs at the END of a build, AFTER all writes and the linker, but BEFORE
activation. It reconciles identity so the new version (by interval membership)
never serves a deleted/renamed artifact or a ghost edge, WITHOUT mutating any
row a prior active version still serves (invariant 5): invalidation is only ever
setting `invalidated_at_seq` (NULL -> this build_seq) and propagating `acl_teams`
onto live artifacts. No live row is physically DELETEd.

Four sub-passes, in order (docs/contracts/version-membership.md):

1. Rename detection — a source vanished but its content_hash reappears at a NEW
   path this build ⇒ link the new artifact to the old (prior_identity_id),
   reattach live edges from old -> new, and invalidate the old artifact. Run
   FIRST so a rename is not mistaken for a deletion.
2. Deletion sweep — any source NOT seen in this build's connector listing ⇒ mark
   is_deleted and invalidate its still-live artifacts + every still-live edge
   touching them; retire their generation/embedding cache rows.
3. Supersession sweep — a source whose CONTENT changed this build (cache miss ⇒
   new artifacts written at this build_seq) ⇒ invalidate its PRIOR-generation live
   artifacts (valid_from_seq < build_seq) and the edges touching them, so the new
   version serves only the new generation, not both. A cache-HIT-carried source is
   NOT in this set, so its artifacts are kept (correct — unchanged content).
4. ACL propagation — a seen source whose acl_teams changed (even content-unchanged
   ⇒ cache hit) ⇒ overwrite its live artifacts' acl_teams this build. Edge ACL is
   the read-time endpoint intersection (acl-source-visibility.md), so a restricted
   endpoint hides its edges automatically.

Idempotency: a rebuild on unchanged inputs sweeps nothing (every source is seen
AND a cache hit, so changed_source_ids is empty), detects no rename, and the ACL
write is a no-op. No churn.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.infrastructure.postgres.models import (
    EmbeddingCache,
    GenerationCacheArtifact,
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class InvalidationResult:
    """Counts for the build runner's structured summary."""

    renames_detected: int = 0
    edges_reattached: int = 0
    sources_deleted: int = 0
    artifacts_invalidated: int = 0
    edges_invalidated: int = 0
    cache_rows_retired: int = 0
    superseded_artifacts_invalidated: int = 0
    acl_sources_propagated: int = 0
    acl_artifacts_updated: int = 0


async def run_invalidation_pass(
    session: AsyncSession,
    *,
    build_seq: int,
    seen_source_ids: set[uuid.UUID],
    changed_source_ids: set[uuid.UUID] | None = None,
) -> InvalidationResult:
    """Reconcile identity for this build; return counts. Flushes, never commits —
    the build runner owns the transaction (the whole pass lands atomically with
    the build's writes, so a crash leaves no half-invalidated version)."""
    changed_source_ids = changed_source_ids or set()
    vanished = await _vanished_live_sources(session, seen_source_ids)
    renames, reattached, renamed_source_ids = await _detect_renames(
        session, build_seq=build_seq, vanished_source_ids=vanished
    )
    # A renamed-away source is invalidated by the rename pass (its old artifacts
    # carry prior_identity_id and are invalidated there), so the deletion sweep
    # skips it — re-invalidating would be a harmless no-op but the skip keeps the
    # delete/rename counts honest.
    to_delete = vanished - renamed_source_ids
    deletion = await _deletion_sweep(session, build_seq=build_seq, source_ids=to_delete)
    superseded = await _supersession_sweep(
        session, build_seq=build_seq, changed_source_ids=changed_source_ids
    )
    acl = await _propagate_acl(session, seen_source_ids=seen_source_ids)
    await session.flush()
    result = InvalidationResult(
        renames_detected=renames,
        edges_reattached=reattached,
        sources_deleted=deletion.sources_deleted,
        artifacts_invalidated=deletion.artifacts_invalidated,
        edges_invalidated=deletion.edges_invalidated,
        cache_rows_retired=deletion.cache_rows_retired,
        superseded_artifacts_invalidated=superseded,
        acl_sources_propagated=acl[0],
        acl_artifacts_updated=acl[1],
    )
    logger.info(
        "event=invalidation_pass_completed build_seq=%d renames=%d edges_reattached=%d "
        "sources_deleted=%d artifacts_invalidated=%d edges_invalidated=%d cache_retired=%d "
        "superseded_artifacts=%d acl_sources=%d acl_artifacts=%d",
        build_seq,
        result.renames_detected,
        result.edges_reattached,
        result.sources_deleted,
        result.artifacts_invalidated,
        result.edges_invalidated,
        result.cache_rows_retired,
        result.superseded_artifacts_invalidated,
        result.acl_sources_propagated,
        result.acl_artifacts_updated,
    )
    return result


async def _supersession_sweep(
    session: AsyncSession, *, build_seq: int, changed_source_ids: set[uuid.UUID]
) -> int:
    """Invalidate the PRIOR generation of a content-changed source's artifacts.

    A content change re-extracts/re-graphifies the source, writing NEW artifacts
    at this build_seq. The previous generation (valid_from_seq < build_seq, still
    live) must leave the KB this build so the new version serves only the new
    generation — but stays a member of every prior version (immutability). Edges
    touching the superseded artifacts are invalidated too (no ghosts)."""
    if not changed_source_ids:
        return 0
    ids = list(changed_source_ids)
    prior = await session.execute(
        select(KnowledgeArtifact.artifact_id).where(
            KnowledgeArtifact.source_id.in_(ids),
            KnowledgeArtifact.valid_from_seq < build_seq,
            KnowledgeArtifact.invalidated_at_seq.is_(None),
        )
    )
    prior_ids = [r.artifact_id for r in prior]
    if not prior_ids:
        return 0
    invalidated = await session.execute(
        update(KnowledgeArtifact)
        .where(
            KnowledgeArtifact.artifact_id.in_(prior_ids),
            KnowledgeArtifact.invalidated_at_seq.is_(None),
        )
        .values(invalidated_at_seq=build_seq)
        .returning(KnowledgeArtifact.artifact_id)
    )
    count = len(invalidated.fetchall())
    await session.execute(
        update(KnowledgeEdge)
        .where(
            KnowledgeEdge.invalidated_at_seq.is_(None),
            (KnowledgeEdge.from_artifact_id.in_(prior_ids))
            | (KnowledgeEdge.to_artifact_id.in_(prior_ids)),
        )
        .values(invalidated_at_seq=build_seq)
    )
    logger.info(
        "event=supersession_sweep build_seq=%d superseded_artifacts=%d",
        build_seq,
        count,
    )
    return count


async def _vanished_live_sources(
    session: AsyncSession, seen_source_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Live (not-yet-deleted) source_items absent from this build's listing."""
    rows = await session.execute(
        select(SourceItem.source_id).where(SourceItem.is_deleted.is_(False))
    )
    return {row.source_id for row in rows} - seen_source_ids


@dataclass
class _DeletionCounts:
    sources_deleted: int = 0
    artifacts_invalidated: int = 0
    edges_invalidated: int = 0
    cache_rows_retired: int = 0


async def _detect_renames(
    session: AsyncSession,
    *,
    build_seq: int,
    vanished_source_ids: set[uuid.UUID],
) -> tuple[int, int, set[uuid.UUID]]:
    """Deterministic rename: a vanished source's still-live artifact whose
    content_hash matches a NEW artifact introduced THIS build (valid_from_seq =
    build_seq) at a DIFFERENT source. Detection is content_hash equality only —
    never a fuzzy guess (invariant 7). Returns (renames, edges_reattached,
    renamed_source_ids)."""
    if not vanished_source_ids:
        return 0, 0, set()

    # Old live artifacts of the vanished sources, by content_hash.
    old_rows = await session.execute(
        select(
            KnowledgeArtifact.artifact_id,
            KnowledgeArtifact.content_hash,
            KnowledgeArtifact.source_id,
        ).where(
            KnowledgeArtifact.source_id.in_(list(vanished_source_ids)),
            KnowledgeArtifact.invalidated_at_seq.is_(None),
            KnowledgeArtifact.content_hash.is_not(None),
        )
    )
    old_by_hash: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
    for row in old_rows:
        # First artifact per hash wins; a duplicate hash within the vanished set is
        # ambiguous, so we leave the extras for the deletion sweep (no fuzzy guess).
        old_by_hash.setdefault(row.content_hash, (row.artifact_id, row.source_id))
    if not old_by_hash:
        return 0, 0, set()

    # New artifacts introduced THIS build whose content_hash matches a vanished
    # one, at a source NOT in the vanished set (a genuinely new path).
    new_rows = await session.execute(
        select(
            KnowledgeArtifact.artifact_id,
            KnowledgeArtifact.content_hash,
            KnowledgeArtifact.source_id,
        ).where(
            KnowledgeArtifact.valid_from_seq == build_seq,
            KnowledgeArtifact.content_hash.in_(list(old_by_hash.keys())),
            KnowledgeArtifact.source_id.notin_(list(vanished_source_ids)),
        )
    )
    renames = 0
    edges_reattached = 0
    renamed_source_ids: set[uuid.UUID] = set()
    matched_hashes: set[str] = set()
    for row in new_rows:
        if row.content_hash in matched_hashes:
            continue  # one new artifact per old identity (deterministic)
        old_artifact_id, old_source_id = old_by_hash[row.content_hash]
        matched_hashes.add(row.content_hash)
        renamed_source_ids.add(old_source_id)
        # Carry the rename link so history survives.
        await session.execute(
            update(KnowledgeArtifact)
            .where(KnowledgeArtifact.artifact_id == row.artifact_id)
            .values(prior_identity_id=old_artifact_id)
        )
        # Reattach still-live edges from the old artifact to the new one so they
        # are not orphaned/ghosted. The linker recomputes its own edges, but
        # graphify edges point at the old artifact id; repoint them.
        edges_reattached += await _reattach_edges(
            session,
            old_artifact_id=old_artifact_id,
            new_artifact_id=row.artifact_id,
            build_seq=build_seq,
        )
        # Invalidate the old artifact (it left the KB this build). Its source is
        # also marked deleted by the deletion sweep's skip — handle it here so the
        # old artifact does not survive in the new version.
        await session.execute(
            update(KnowledgeArtifact)
            .where(
                KnowledgeArtifact.artifact_id == old_artifact_id,
                KnowledgeArtifact.invalidated_at_seq.is_(None),
            )
            .values(invalidated_at_seq=build_seq)
        )
        await session.execute(
            update(SourceItem).where(SourceItem.source_id == old_source_id).values(is_deleted=True)
        )
        renames += 1
        logger.info(
            "event=rename_detected build_seq=%d old_artifact_id=%s new_artifact_id=%s "
            "content_hash=%s edges_reattached=%d",
            build_seq,
            old_artifact_id,
            row.artifact_id,
            row.content_hash,
            edges_reattached,
        )
    return renames, edges_reattached, renamed_source_ids


async def _reattach_edges(
    session: AsyncSession,
    *,
    old_artifact_id: uuid.UUID,
    new_artifact_id: uuid.UUID,
    build_seq: int,
) -> int:
    """Repoint still-live edges incident on the old artifact onto the new one.

    For each incident live edge, swap the old endpoint for the new one. An edge
    whose OTHER endpoint is already the new artifact would become a self-loop on
    repoint — that edge merely linked the two identities of the same renamed
    entity, so it is invalidated rather than repointed. Invalidating it (instead
    of leaving it untouched) is required: the old artifact is invalidated right
    after, so a skipped edge still pointing at it would be a ghost the no-ghost
    gate rejects. Duplicate graphify edges a repoint may produce are tolerated —
    graph BFS dedups by visited-set, so they are benign at read time.
    """
    reattached = 0
    for column, other in (
        (KnowledgeEdge.from_artifact_id, KnowledgeEdge.to_artifact_id),
        (KnowledgeEdge.to_artifact_id, KnowledgeEdge.from_artifact_id),
    ):
        # Would-be self-loops: invalidate instead of repointing (no ghost, no loop).
        await session.execute(
            update(KnowledgeEdge)
            .where(
                column == old_artifact_id,
                other == new_artifact_id,
                KnowledgeEdge.invalidated_at_seq.is_(None),
            )
            .values(invalidated_at_seq=build_seq)
        )
        # The rest repoint onto the new artifact.
        result = await session.execute(
            update(KnowledgeEdge)
            .where(
                column == old_artifact_id,
                other != new_artifact_id,
                KnowledgeEdge.invalidated_at_seq.is_(None),
            )
            .values(**{column.key: new_artifact_id})
            .returning(KnowledgeEdge.edge_id)
        )
        reattached += len(result.fetchall())
    return reattached


async def _deletion_sweep(
    session: AsyncSession, *, build_seq: int, source_ids: set[uuid.UUID]
) -> _DeletionCounts:
    """Mark vanished sources deleted; invalidate their live artifacts + incident
    live edges; retire their generation/embedding cache rows."""
    counts = _DeletionCounts()
    if not source_ids:
        return counts
    ids = list(source_ids)

    # Live artifacts of the deleted sources.
    artifact_rows = await session.execute(
        select(KnowledgeArtifact.artifact_id).where(
            KnowledgeArtifact.source_id.in_(ids),
            KnowledgeArtifact.invalidated_at_seq.is_(None),
        )
    )
    artifact_ids = [r.artifact_id for r in artifact_rows]

    counts.sources_deleted = len(ids)
    await session.execute(
        update(SourceItem).where(SourceItem.source_id.in_(ids)).values(is_deleted=True)
    )
    if artifact_ids:
        invalidated = await session.execute(
            update(KnowledgeArtifact)
            .where(
                KnowledgeArtifact.artifact_id.in_(artifact_ids),
                KnowledgeArtifact.invalidated_at_seq.is_(None),
            )
            .values(invalidated_at_seq=build_seq)
            .returning(KnowledgeArtifact.artifact_id)
        )
        counts.artifacts_invalidated = len(invalidated.fetchall())
        # Every still-live edge touching a now-invalid artifact leaves the KB too.
        edges = await session.execute(
            update(KnowledgeEdge)
            .where(
                KnowledgeEdge.invalidated_at_seq.is_(None),
                (KnowledgeEdge.from_artifact_id.in_(artifact_ids))
                | (KnowledgeEdge.to_artifact_id.in_(artifact_ids)),
            )
            .values(invalidated_at_seq=build_seq)
            .returning(KnowledgeEdge.edge_id)
        )
        counts.edges_invalidated = len(edges.fetchall())
        counts.cache_rows_retired = await _retire_caches(session, artifact_ids)
    logger.info(
        "event=deletion_sweep build_seq=%d sources_deleted=%d artifacts_invalidated=%d "
        "edges_invalidated=%d cache_retired=%d",
        build_seq,
        counts.sources_deleted,
        counts.artifacts_invalidated,
        counts.edges_invalidated,
        counts.cache_rows_retired,
    )
    return counts


async def _retire_caches(session: AsyncSession, artifact_ids: Iterable[uuid.UUID]) -> int:
    """Retire generation/embedding cache rows for invalidated artifacts.

    A deleted artifact must not satisfy a future cache hit (it would re-introduce
    a dead row). generation_cache_artifact rows are removed (the generation_cache
    row CASCADE-removes when its last mapping is gone is NOT relied on; we delete
    the mapping so a future lookup_artifact_ids returns empty and forces a fresh
    generate). embedding_cache rows are removed so the artifact is not re-embedded.
    """
    ids = list(artifact_ids)
    if not ids:
        return 0
    gca = await session.execute(
        delete(GenerationCacheArtifact)
        .where(GenerationCacheArtifact.artifact_id.in_(ids))
        .returning(GenerationCacheArtifact.artifact_id)
    )
    emb = await session.execute(
        delete(EmbeddingCache)
        .where(EmbeddingCache.artifact_id.in_(ids))
        .returning(EmbeddingCache.artifact_id)
    )
    return len(gca.fetchall()) + len(emb.fetchall())


async def _propagate_acl(
    session: AsyncSession, *, seen_source_ids: set[uuid.UUID]
) -> tuple[int, int]:
    """Propagate each seen source's acl_teams onto its live artifacts.

    Closes the acl-propagation write: an ACL-only config change lands even on a
    cache hit (content_hash unchanged), because _touch_last_seen already wrote the
    new acl_teams onto source_item. Only updates artifacts whose acl_teams differs
    from the source (idempotent: unchanged ACL ⇒ zero rows updated, no churn).
    Edge ACL is the read-time endpoint intersection, so no edge rewrite is needed.
    """
    if not seen_source_ids:
        return 0, 0
    rows = await session.execute(
        select(SourceItem.source_id, SourceItem.acl_teams).where(
            SourceItem.source_id.in_(list(seen_source_ids))
        )
    )
    sources_propagated = 0
    artifacts_updated = 0
    for source_id, acl_teams in rows:
        result = await session.execute(
            update(KnowledgeArtifact)
            .where(
                KnowledgeArtifact.source_id == source_id,
                KnowledgeArtifact.invalidated_at_seq.is_(None),
                KnowledgeArtifact.acl_teams != list(acl_teams),
            )
            .values(acl_teams=list(acl_teams))
            .returning(KnowledgeArtifact.artifact_id)
        )
        updated = len(result.fetchall())
        if updated:
            sources_propagated += 1
            artifacts_updated += updated
            logger.info(
                "event=acl_propagated source_id=%s acl_teams=%s artifacts_updated=%d",
                source_id,
                list(acl_teams),
                updated,
            )
    return sources_propagated, artifacts_updated


__all__ = ["InvalidationResult", "run_invalidation_pass"]
