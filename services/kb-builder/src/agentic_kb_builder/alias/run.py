"""Build-time alias miner pass (PR-38, docs/contracts/alias-reference.md).

Reconciles the live `alias_reference` artifact set + `aliases` edges against the
live post-invalidation registry, mirroring the linker's derived-set semantics:
one row per logical alias phrase, refreshed in place (original valid_from_seq
kept), stale rows/edges soft-invalidated at this build_seq — never physically
deleted. Runs AFTER the invalidation pass (a sweep-invalidated alias that is
still confirmed is revived in the same transaction) and BEFORE centrality.

Incremental skip: each alias row's evidence entries carry the content_hash the
contributing source was mined at. A live source whose artifact hash is unchanged
is NOT re-mined — its stored contribution is replayed (event=alias_mining_source
decision=skip_unchanged), exactly like docify/graphify skip on cache hit. Zero
LLM calls, zero embeddings anywhere in this pass.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.alias.mining import (
    AliasAggregate,
    MinedPhrase,
    SourceContribution,
    aggregate_contributions,
    mine_commit,
    mine_doc_source,
    phrase_variants,
)
from agentic_kb_builder.alias.resolve import AliasEntry
from agentic_kb_builder.connectors.git_metadata import parse_changed_files
from agentic_kb_builder.domain.acl_intersection import commit_acl_intersection
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.domain.schema_versions import RELATION_SCHEMA_VERSION
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Mirrors application.write_commit.COMMIT_ARTIFACT_TYPE / linker.records.COMMIT_ARTIFACT_TYPE
# (same trivial string literal, duplicated rather than imported: alias.run must not import
# anything under agentic_kb_builder.application — application/__init__ imports build_runner,
# which wires in the alias miner, so importing across that boundary is a circular import).
COMMIT_ARTIFACT_TYPE = "commit"
ALIAS_ARTIFACT_TYPE = "alias_reference"
ALIAS_BODY_SCHEMA = "alias_reference_v1"
ALIAS_EDGE_TYPE = "aliases"
ALIAS_EDGE_SOURCE = "alias_miner"
# Deterministic producers may only ever assign EXTRACTED (trust-buckets.md);
# routing-only semantics are carried by the ontology row, not the trust class.
ALIAS_EDGE_TRUST_CLASS = "EXTRACTED"
# Edge fan-out cap: an alias naming more targets than this is too diffuse for
# graph routing; the full ranked list (up to MAX_BODY_TARGETS) stays in the body.
MAX_EDGE_TARGETS = 20
# Deterministic preference when a target path resolves to several live artifacts.
_TARGET_TYPE_PREFERENCE = {"code_file": 0, "summary": 1}


@dataclass(frozen=True)
class AliasMinerResult:
    """Structured counters for the build log + tests."""

    sources_seen: int = 0
    sources_mined: int = 0
    sources_skipped_unchanged: int = 0
    phrases: int = 0
    artifacts_inserted: int = 0
    artifacts_refreshed: int = 0
    artifacts_unchanged: int = 0
    artifacts_invalidated: int = 0
    edges_inserted: int = 0
    edges_refreshed: int = 0
    edges_unchanged: int = 0
    edges_invalidated: int = 0


@dataclass(frozen=True)
class _MiningInput:
    """One live source eligible for mining, with everything extraction needs."""

    source_key: str
    ref: str
    content_hash: str
    source_id: uuid.UUID
    extract_kind: str  # "commit" | "doc"
    subject: str = ""
    changed_files: tuple[str, ...] = ()
    doc_path: str = ""


async def run_alias_miner(
    session: AsyncSession, *, kb_version: str, valid_from_seq: int
) -> AliasMinerResult:
    """Execute the alias reconcile for this build. Flushes, never commits — the
    build runner owns the transaction (same policy as linker/invalidation)."""
    inputs = await _load_mining_inputs(session)
    prior_rows = await _load_prior_alias_rows(session, valid_from_seq)
    prior_extractions = _prior_extractions(prior_rows)

    contributions: list[SourceContribution] = []
    mined = 0
    skipped = 0
    for mining_input in inputs:
        prior = prior_extractions.get(mining_input.source_key)
        if prior is not None and prior[0] == mining_input.content_hash:
            phrases = tuple(MinedPhrase(phrase=p, targets=t) for p, t in sorted(prior[1].items()))
            skipped += 1
            decision = "skip_unchanged"
        else:
            phrases = _extract(mining_input)
            mined += 1
            decision = "mined"
        logger.info(
            "event=alias_mining_source source=%s decision=%s phrases=%d content_hash=%s",
            mining_input.source_key,
            decision,
            len(phrases),
            mining_input.content_hash,
        )
        contributions.append(
            SourceContribution(
                source_key=mining_input.source_key,
                ref=mining_input.ref,
                content_hash=mining_input.content_hash,
                phrases=phrases,
            )
        )

    aggregates = aggregate_contributions(contributions)
    anchor_by_key = {i.source_key: i.source_id for i in inputs}
    target_paths = {t.path for agg in aggregates for t in agg.targets}
    path_acls, path_artifacts = await _resolve_targets(session, target_paths)

    art_counts, alias_ids = await _reconcile_artifacts(
        session,
        aggregates=aggregates,
        prior_rows=prior_rows,
        anchor_by_key=anchor_by_key,
        path_acls=path_acls,
        path_artifacts=path_artifacts,
        kb_version=kb_version,
        valid_from_seq=valid_from_seq,
    )
    edge_counts = await _reconcile_edges(
        session,
        aggregates=aggregates,
        alias_ids=alias_ids,
        path_artifacts=path_artifacts,
        kb_version=kb_version,
        valid_from_seq=valid_from_seq,
    )
    await session.flush()
    result = AliasMinerResult(
        sources_seen=len(inputs),
        sources_mined=mined,
        sources_skipped_unchanged=skipped,
        phrases=len(aggregates),
        artifacts_inserted=art_counts[0],
        artifacts_refreshed=art_counts[1],
        artifacts_unchanged=art_counts[2],
        artifacts_invalidated=art_counts[3],
        edges_inserted=edge_counts[0],
        edges_refreshed=edge_counts[1],
        edges_unchanged=edge_counts[2],
        edges_invalidated=edge_counts[3],
    )
    logger.info(
        "event=alias_miner_completed kb_version=%s build_seq=%d sources_seen=%d "
        "sources_mined=%d sources_skipped_unchanged=%d phrases=%d artifacts_inserted=%d "
        "artifacts_refreshed=%d artifacts_unchanged=%d artifacts_invalidated=%d "
        "edges_inserted=%d edges_refreshed=%d edges_unchanged=%d edges_invalidated=%d",
        kb_version,
        valid_from_seq,
        result.sources_seen,
        result.sources_mined,
        result.sources_skipped_unchanged,
        result.phrases,
        result.artifacts_inserted,
        result.artifacts_refreshed,
        result.artifacts_unchanged,
        result.artifacts_invalidated,
        result.edges_inserted,
        result.edges_refreshed,
        result.edges_unchanged,
        result.edges_invalidated,
    )
    return result


def _extract(mining_input: _MiningInput) -> tuple[MinedPhrase, ...]:
    if mining_input.extract_kind == "commit":
        return mine_commit(mining_input.subject, mining_input.changed_files)
    return mine_doc_source(mining_input.doc_path)


async def _load_mining_inputs(session: AsyncSession) -> list[_MiningInput]:
    """Live commit artifacts + live markdown doc sources — everything the miner
    needs is already in Postgres (subject + changed files live in the commit
    artifact's body_text; parse_changed_files recovers the file list)."""
    inputs: list[_MiningInput] = []
    commit_rows = await session.execute(
        select(
            KnowledgeArtifact.body_text,
            KnowledgeArtifact.content_hash,
            KnowledgeArtifact.source_id,
            SourceItem.source_uri,
            SourceItem.source_version,
        )
        .join(SourceItem, KnowledgeArtifact.source_id == SourceItem.source_id)
        .where(
            KnowledgeArtifact.artifact_type == COMMIT_ARTIFACT_TYPE,
            KnowledgeArtifact.invalidated_at_seq.is_(None),
            SourceItem.is_deleted.is_(False),
        )
    )
    for body_text, body_hash, source_id, source_uri, source_version in commit_rows.tuples():
        if body_text is None or body_hash is None:
            continue
        inputs.append(
            _MiningInput(
                source_key=f"commit:{source_uri}",
                ref=(source_version or "")[:12],
                content_hash=body_hash,
                source_id=source_id,
                extract_kind="commit",
                subject=body_text.split("\n", 1)[0].strip(),
                changed_files=parse_changed_files(body_text),
            )
        )
    doc_rows = await session.execute(
        select(
            SourceItem.source_uri,
            SourceItem.path,
            SourceItem.content_hash,
            SourceItem.source_id,
        ).where(
            SourceItem.source_type == "github_doc",
            SourceItem.is_deleted.is_(False),
            SourceItem.path.is_not(None),
        )
    )
    for source_uri, path, source_hash, source_id in doc_rows.tuples():
        if path is None or source_hash is None or not path.lower().endswith(".md"):
            continue
        inputs.append(
            _MiningInput(
                source_key=f"doc:{source_uri}",
                ref=path,
                content_hash=source_hash,
                source_id=source_id,
                extract_kind="doc",
                doc_path=path,
            )
        )
    inputs.sort(key=lambda i: i.source_key)
    return inputs


async def _load_prior_alias_rows(
    session: AsyncSession, valid_from_seq: int
) -> dict[str, KnowledgeArtifact]:
    """Live alias rows plus rows the sweeps invalidated THIS build (revival
    candidates — the sweep's write never became visible outside this
    transaction). Keyed by title (one live row per phrase, reconcile-enforced)."""
    rows = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(
                    KnowledgeArtifact.artifact_type == ALIAS_ARTIFACT_TYPE,
                    or_(
                        KnowledgeArtifact.invalidated_at_seq.is_(None),
                        KnowledgeArtifact.invalidated_at_seq == valid_from_seq,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    by_title: dict[str, KnowledgeArtifact] = {}
    for row in sorted(rows, key=lambda r: (r.invalidated_at_seq is not None, str(r.artifact_id))):
        if row.title is None:
            continue
        if row.title in by_title:
            # Duplicate logical alias (should not happen — single nightly runner).
            # Keep the live/first row; invalidate the extra so the set self-heals.
            logger.warning(
                "event=alias_duplicate_row title=%s kept=%s dropped=%s",
                row.title,
                by_title[row.title].artifact_id,
                row.artifact_id,
            )
            if row.invalidated_at_seq is None:
                row.invalidated_at_seq = valid_from_seq
            continue
        by_title[row.title] = row
    return by_title


def _prior_extractions(
    prior_rows: dict[str, KnowledgeArtifact],
) -> dict[str, tuple[str, dict[str, tuple[str, ...]]]]:
    """Rebuild per-source extraction state from stored evidence entries:
    source_key -> (content_hash mined at, {phrase: targets}). This is the
    incremental-skip watermark (alias-reference.md)."""
    state: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {}
    for title, row in sorted(prior_rows.items()):
        body = _parse_body(row)
        if body is None:
            continue
        for entry in body.get("evidence", []):
            source_key = entry.get("source")
            mined_hash = entry.get("content_hash")
            targets = tuple(entry.get("targets", []))
            if not isinstance(source_key, str) or not isinstance(mined_hash, str):
                continue
            known = state.get(source_key)
            if known is None or known[0] == mined_hash:
                phrases = known[1] if known is not None else {}
                phrases[title] = targets
                state[source_key] = (mined_hash, phrases)
            # A hash mismatch between rows for one source means stale state; the
            # source re-mines (treated as changed) because reuse requires ALL
            # stored entries to agree on the mined-at hash.
            elif known[0] != mined_hash:
                state.pop(source_key, None)
                logger.warning("event=alias_watermark_conflict source=%s", source_key)
    return state


def _parse_body(row: KnowledgeArtifact) -> dict[str, Any] | None:
    if row.body_text is None:
        return None
    try:
        body = json.loads(row.body_text)
    except ValueError:
        logger.warning(
            "event=alias_body_unparseable artifact_id=%s title=%s", row.artifact_id, row.title
        )
        return None
    return body if isinstance(body, dict) else None


async def _resolve_targets(
    session: AsyncSession, paths: set[str]
) -> tuple[dict[str, list[str]], dict[str, uuid.UUID]]:
    """Map target path -> (source ACL, preferred live artifact id).

    ACL per path is strictest-wins across matching source rows (mirrors
    write_commit._file_acls); the artifact preference is code_file, then
    summary, then any — deterministic. Paths with no live source stay absent
    (they contribute nothing to ACL — deny-by-default handles zero-resolution —
    and get no edge, so the no-ghost gate holds).
    """
    if not paths:
        return {}, {}
    source_rows = await session.execute(
        select(SourceItem.source_id, SourceItem.path, SourceItem.acl_teams).where(
            SourceItem.path.in_(sorted(paths)),
            SourceItem.is_deleted.is_(False),
        )
    )
    acls: dict[str, list[str]] = {}
    source_ids: dict[uuid.UUID, str] = {}
    for source_id, path, acl_teams in source_rows.tuples():
        if path is None:
            continue
        source_ids[source_id] = path
        teams = list(acl_teams)
        existing = acls.get(path)
        if existing is None:
            acls[path] = teams
        elif existing and teams:
            acls[path] = sorted(set(existing) & set(teams))
        else:
            acls[path] = existing or teams
    if not source_ids:
        return acls, {}
    artifact_rows = await session.execute(
        select(
            KnowledgeArtifact.artifact_id,
            KnowledgeArtifact.artifact_type,
            KnowledgeArtifact.source_id,
        ).where(
            KnowledgeArtifact.source_id.in_(list(source_ids)),
            KnowledgeArtifact.invalidated_at_seq.is_(None),
            KnowledgeArtifact.artifact_type != ALIAS_ARTIFACT_TYPE,
        )
    )
    best: dict[str, tuple[int, str, uuid.UUID]] = {}
    for artifact_id, artifact_type, source_id in artifact_rows.tuples():
        path = source_ids.get(source_id)
        if path is None:
            continue
        key = (
            _TARGET_TYPE_PREFERENCE.get(artifact_type, len(_TARGET_TYPE_PREFERENCE)),
            str(artifact_id),
            artifact_id,
        )
        current = best.get(path)
        if current is None or key[:2] < current[:2]:
            best[path] = key
    return acls, {path: key[2] for path, key in best.items()}


def _desired_body(aggregate: AliasAggregate, path_artifacts: dict[str, uuid.UUID]) -> str:
    body = {
        "schema": ALIAS_BODY_SCHEMA,
        "alias": aggregate.phrase,
        "variants": list(phrase_variants(aggregate.phrase)),
        "confidence_tier": "interpreted",
        "confirmation_count": aggregate.confirmation_count,
        "targets": [
            {
                "path": target.path,
                "artifact_id": (
                    str(path_artifacts[target.path]) if target.path in path_artifacts else None
                ),
                "count": target.count,
            }
            for target in aggregate.targets
        ],
        "evidence": [
            {"source": source, "ref": ref, "content_hash": mined_hash, "targets": list(targets)}
            for source, ref, mined_hash, targets in aggregate.evidence
        ],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def _reconcile_artifacts(
    session: AsyncSession,
    *,
    aggregates: tuple[AliasAggregate, ...],
    prior_rows: dict[str, KnowledgeArtifact],
    anchor_by_key: dict[str, uuid.UUID],
    path_acls: dict[str, list[str]],
    path_artifacts: dict[str, uuid.UUID],
    kb_version: str,
    valid_from_seq: int,
) -> tuple[tuple[int, int, int, int], dict[str, uuid.UUID]]:
    """Upsert one row per phrase; soft-invalidate live rows no longer mined.
    Returns ((inserted, refreshed, unchanged, invalidated), phrase -> artifact_id)."""
    inserted = refreshed = unchanged = 0
    alias_ids: dict[str, uuid.UUID] = {}
    desired_titles: set[str] = set()
    new_rows: list[tuple[str, KnowledgeArtifact]] = []
    for aggregate in aggregates:
        desired_titles.add(aggregate.phrase)
        body_text = _desired_body(aggregate, path_artifacts)
        target_paths = [t.path for t in aggregate.targets]
        acl_teams = commit_acl_intersection(target_paths, path_acls)
        search_text = " ".join((aggregate.phrase, *phrase_variants(aggregate.phrase)))
        # Anchor: the lexicographically-first contributing source (stable).
        anchor_key = aggregate.evidence[0][0]
        anchor_id = anchor_by_key.get(anchor_key)
        if anchor_id is None:
            # Contribution replayed from stored evidence must come from a live
            # input, so this is unreachable in practice; skip loudly if not.
            logger.warning(
                "event=alias_anchor_missing phrase=%s source=%s", aggregate.phrase, anchor_key
            )
            continue
        row = prior_rows.get(aggregate.phrase)
        if row is None:
            row = KnowledgeArtifact(
                artifact_type=ALIAS_ARTIFACT_TYPE,
                source_id=anchor_id,
                title=aggregate.phrase,
                body_text=body_text,
                content_hash=content_hash(body_text),
                search_text=search_text,
                kb_version=kb_version,
                valid_from_seq=valid_from_seq,
                knowledge_kind="interpreted",
                acl_teams=acl_teams,
            )
            session.add(row)
            new_rows.append((aggregate.phrase, row))
            inserted += 1
            continue
        alias_ids[aggregate.phrase] = row.artifact_id
        same = (
            row.body_text == body_text
            and row.search_text == search_text
            and list(row.acl_teams) == acl_teams
            and row.source_id == anchor_id
            and row.invalidated_at_seq is None
        )
        if same:
            unchanged += 1
            continue
        # In-place refresh of a derived, reconciled row (linker-edge precedent):
        # keep the original valid_from_seq; revive a same-build sweep invalidation.
        row.body_text = body_text
        row.content_hash = content_hash(body_text)
        row.search_text = search_text
        row.acl_teams = acl_teams
        row.source_id = anchor_id
        row.kb_version = kb_version
        row.invalidated_at_seq = None
        refreshed += 1
    invalidated = 0
    for title, row in sorted(prior_rows.items()):
        if title in desired_titles or row.invalidated_at_seq is not None:
            continue
        row.invalidated_at_seq = valid_from_seq
        invalidated += 1
        logger.info(
            "event=alias_invalidated reason=no_contributing_source title=%s artifact_id=%s "
            "invalidated_at_seq=%d",
            title,
            row.artifact_id,
            valid_from_seq,
        )
    await session.flush()
    for phrase, row in new_rows:
        alias_ids[phrase] = row.artifact_id
    return (inserted, refreshed, unchanged, invalidated), alias_ids


async def _reconcile_edges(
    session: AsyncSession,
    *,
    aggregates: tuple[AliasAggregate, ...],
    alias_ids: dict[str, uuid.UUID],
    path_artifacts: dict[str, uuid.UUID],
    kb_version: str,
    valid_from_seq: int,
) -> tuple[int, int, int, int]:
    """Reconcile `aliases` edges (source='alias_miner') against the desired set.

    Idempotency is reconciliation against the computed set (no partial unique
    index needed — the pass owns every alias_miner edge and the nightly runner
    is single-writer): insert missing, refresh/revive changed, soft-invalidate
    stale. Only resolved live targets get edges (no dangling endpoints).
    """
    desired: dict[tuple[uuid.UUID, uuid.UUID], tuple[float, dict[str, Any]]] = {}
    for aggregate in aggregates:
        alias_id = alias_ids.get(aggregate.phrase)
        if alias_id is None:
            continue
        refs_by_path: dict[str, list[str]] = {}
        for _, ref, _, targets in aggregate.evidence:
            for path in targets:
                refs_by_path.setdefault(path, []).append(ref)
        for target in aggregate.targets[:MAX_EDGE_TARGETS]:
            target_id = path_artifacts.get(target.path)
            if target_id is None:
                continue
            confidence = target.count / aggregate.confirmation_count
            evidence = {
                "alias": aggregate.phrase,
                "target_path": target.path,
                "sources": sorted(refs_by_path.get(target.path, [])),
            }
            desired[(alias_id, target_id)] = (confidence, evidence)
    existing_rows = (
        (
            await session.execute(
                select(KnowledgeEdge).where(
                    KnowledgeEdge.source == ALIAS_EDGE_SOURCE,
                    KnowledgeEdge.edge_type == ALIAS_EDGE_TYPE,
                    or_(
                        KnowledgeEdge.invalidated_at_seq.is_(None),
                        KnowledgeEdge.invalidated_at_seq == valid_from_seq,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    inserted = refreshed = unchanged = invalidated = 0
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for edge in sorted(existing_rows, key=lambda e: str(e.edge_id)):
        key = (edge.from_artifact_id, edge.to_artifact_id)
        if key not in desired or key in seen:
            if edge.invalidated_at_seq is None:
                edge.invalidated_at_seq = valid_from_seq
                invalidated += 1
            continue
        seen.add(key)
        confidence, evidence = desired[key]
        same = (
            edge.confidence == confidence
            and edge.evidence == evidence
            and edge.invalidated_at_seq is None
        )
        if same:
            unchanged += 1
            continue
        edge.confidence = confidence
        edge.evidence = evidence
        edge.kb_version = kb_version
        edge.relation_schema_version = RELATION_SCHEMA_VERSION
        edge.invalidated_at_seq = None
        refreshed += 1
    for key in sorted(desired.keys() - seen, key=lambda k: (str(k[0]), str(k[1]))):
        confidence, evidence = desired[key]
        session.add(
            KnowledgeEdge(
                from_artifact_id=key[0],
                to_artifact_id=key[1],
                edge_type=ALIAS_EDGE_TYPE,
                confidence=confidence,
                source=ALIAS_EDGE_SOURCE,
                kb_version=kb_version,
                trust_class=ALIAS_EDGE_TRUST_CLASS,
                relation_schema_version=RELATION_SCHEMA_VERSION,
                evidence=evidence,
                valid_from_seq=valid_from_seq,
            )
        )
        inserted += 1
    await session.flush()
    return inserted, refreshed, unchanged, invalidated


async def load_alias_entries(session: AsyncSession) -> list[AliasEntry]:
    """Load the live alias index as resolver entries (used by the eval runner
    `scripts/eval_alias_resolution.py` and DB-backed tests)."""
    rows = await session.execute(
        select(KnowledgeArtifact.title, KnowledgeArtifact.body_text).where(
            KnowledgeArtifact.artifact_type == ALIAS_ARTIFACT_TYPE,
            KnowledgeArtifact.invalidated_at_seq.is_(None),
        )
    )
    entries: list[AliasEntry] = []
    for title, body_text in rows.tuples():
        if title is None or body_text is None:
            continue
        try:
            body = json.loads(body_text)
        except ValueError:
            continue
        targets = tuple(
            t["path"] for t in body.get("targets", []) if isinstance(t, dict) and "path" in t
        )
        entries.append(
            AliasEntry(
                alias=title,
                targets=targets,
                confirmation_count=int(body.get("confirmation_count", 1)),
            )
        )
    entries.sort(key=lambda e: e.alias)
    return entries


__all__ = [
    "ALIAS_ARTIFACT_TYPE",
    "ALIAS_EDGE_SOURCE",
    "ALIAS_EDGE_TYPE",
    "MAX_EDGE_TARGETS",
    "AliasMinerResult",
    "load_alias_entries",
    "run_alias_miner",
]
