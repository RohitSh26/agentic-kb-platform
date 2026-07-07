"""Ledger-mined alias corrections (PR-43, ADR-0034, docs/contracts/alias-reference.md
"Ledger-mined aliases"). Zero-LLM, deterministic: mines `retrieval_event` MISSES —
`kb_search` calls that came back empty/thin — into `alias_reference` rows so the
exact phrase a developer typed and missed resolves on the next build.

Wired into `build_runner.py` immediately AFTER `alias.run.run_alias_miner` and
BEFORE graph centrality (same `_finalize_graph` phase). Reuses PR-38's alias
machinery verbatim: `alias.mining.normalize_phrase` / `phrase_variants` for
normalization, `alias.resolve.resolve` (unmodified) for candidate matching, and
`domain.acl_intersection.commit_acl_intersection` for the never-widen ACL rule.
No new matching code; no LLM calls; no `retrieval_event` writes (kb-builder never
writes that table — mcp-server's ledger, `postgres-knowledge-registry.md`) and no
`retrieval_event` row is ever modified (read-only input).
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.alias.mining import normalize_phrase, phrase_variants
from agentic_kb_builder.alias.resolve import AliasEntry, resolve
from agentic_kb_builder.alias.run import (
    ALIAS_ARTIFACT_TYPE,
    ALIAS_BODY_SCHEMA,
    LEDGER_MINED_PROVENANCE,
)
from agentic_kb_builder.domain.acl_intersection import commit_acl_intersection
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import (
    KnowledgeArtifact,
    RetrievalEvent,
    SourceItem,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Trailing window over retrieval_event misses (ADR-0034: "window configurable
# (default 14 days)").
DEFAULT_WINDOW_DAYS = 14

# Untrusted-content handling for query_text (alias-reference.md "Ledger-mined
# aliases"): strip ASCII control chars, then cap the RAW phrase length BEFORE
# normalization (a query is a search string, never executed or templated).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MAX_RAW_QUERY_CHARS = 80

# Deterministic preference when several live candidate artifacts share a target
# path (e.g. multiple symbols in one file) — mirrors alias/run.py's
# _TARGET_TYPE_PREFERENCE (small, self-contained; not imported, see module note
# in alias/run.py about avoiding a reverse dependency).
_TARGET_TYPE_PREFERENCE = {"code_file": 0, "summary": 1}


@dataclass(frozen=True)
class LedgerMiningResult:
    """Structured counters for the build log + tests."""

    misses_seen: int = 0
    phrases_seen: int = 0
    mined: int = 0
    already_aliased: int = 0
    unresolved: int = 0
    artifacts_inserted: int = 0
    artifacts_refreshed: int = 0
    artifacts_unchanged: int = 0
    artifacts_invalidated: int = 0


@dataclass(frozen=True)
class _MissGroup:
    """One normalized miss phrase aggregated across the window."""

    phrase: str
    first_seen: datetime
    last_seen: datetime
    miss_count: int
    confirmation_count: int  # distinct UTC calendar days the phrase missed


def _sanitize_query_text(raw: str) -> str:
    """Untrusted-content handling: strip control chars, then length-cap the RAW
    text BEFORE normalization (alias-reference.md "Ledger-mined aliases")."""
    return _CONTROL_CHARS.sub("", raw)[:MAX_RAW_QUERY_CHARS]


async def _load_miss_groups(
    session: AsyncSession, *, window_start: datetime
) -> tuple[list[_MissGroup], int]:
    """Recent kb_search misses, grouped by normalized phrase. Reuses the EXACT
    zero/thin predicate migration 0020's `kb_search_zero_thin` column uses, so the
    two can never disagree. Read-only: retrieval_event rows are never written or
    modified here."""
    rows = await session.execute(
        select(RetrievalEvent.query_text, RetrievalEvent.created_at).where(
            RetrievalEvent.tool_name == "kb_search",
            RetrievalEvent.status == "approved",
            func.coalesce(func.cardinality(RetrievalEvent.returned_artifact_ids), 0) <= 1,
            RetrievalEvent.created_at >= window_start,
            RetrievalEvent.query_text.is_not(None),
        )
    )
    misses_seen = 0
    by_phrase: dict[str, list[datetime]] = {}
    for query_text, created_at in rows.tuples():
        misses_seen += 1
        if query_text is None:
            continue  # excluded by the WHERE clause; narrows the type for pyright
        phrase = normalize_phrase(_sanitize_query_text(query_text))
        if not phrase:
            continue
        by_phrase.setdefault(phrase, []).append(created_at)
    groups = [
        _MissGroup(
            phrase=phrase,
            first_seen=min(timestamps),
            last_seen=max(timestamps),
            miss_count=len(timestamps),
            confirmation_count=len({t.date() for t in timestamps}),
        )
        for phrase, timestamps in sorted(by_phrase.items())
    ]
    return groups, misses_seen


@dataclass(frozen=True)
class _Candidates:
    entries: list[AliasEntry]
    path_artifact: dict[str, uuid.UUID]
    path_acl: dict[str, list[str]]
    path_source: dict[str, uuid.UUID]


async def _load_candidates(session: AsyncSession) -> _Candidates:
    """Every live, non-alias_reference artifact with a title + resolvable source
    path becomes a single-target AliasEntry (alias-reference.md "Candidate
    matching — zero new matching code"). Deterministic entry order (sorted by
    alias then target) so `resolve()`'s exact-match branch — which returns on the
    FIRST equal entry, no further tie-break — is reproducible."""
    rows = await session.execute(
        select(
            KnowledgeArtifact.artifact_id,
            KnowledgeArtifact.artifact_type,
            KnowledgeArtifact.title,
            SourceItem.path,
            SourceItem.acl_teams,
            SourceItem.source_id,
        )
        .join(SourceItem, KnowledgeArtifact.source_id == SourceItem.source_id)
        .where(
            KnowledgeArtifact.artifact_type != ALIAS_ARTIFACT_TYPE,
            KnowledgeArtifact.invalidated_at_seq.is_(None),
            KnowledgeArtifact.title.is_not(None),
            SourceItem.path.is_not(None),
            SourceItem.is_deleted.is_(False),
        )
    )
    entries: list[AliasEntry] = []
    preference: dict[str, tuple[int, str]] = {}
    path_artifact: dict[str, uuid.UUID] = {}
    path_acl: dict[str, list[str]] = {}
    path_source: dict[str, uuid.UUID] = {}
    for artifact_id, artifact_type, title, path, acl_teams, source_id in rows.tuples():
        if title is None or path is None:
            continue  # excluded by the WHERE clause; narrows the type for pyright
        alias = normalize_phrase(title)
        if not alias:
            continue
        entries.append(AliasEntry(alias=alias, targets=(path,), confirmation_count=1))
        type_rank = _TARGET_TYPE_PREFERENCE.get(artifact_type, len(_TARGET_TYPE_PREFERENCE))
        key = (type_rank, str(artifact_id))
        if path not in preference or key < preference[path]:
            preference[path] = key
            path_artifact[path] = artifact_id
            path_acl[path] = list(acl_teams)
            path_source[path] = source_id
    entries.sort(key=lambda e: (e.alias, e.targets))
    return _Candidates(
        entries=entries, path_artifact=path_artifact, path_acl=path_acl, path_source=path_source
    )


def _parse_body(row: KnowledgeArtifact) -> dict[str, Any] | None:
    if row.body_text is None:
        return None
    try:
        body = json.loads(row.body_text)
    except ValueError:
        logger.warning(
            "event=ledger_mining_body_unparseable artifact_id=%s title=%s",
            row.artifact_id,
            row.title,
        )
        return None
    return body if isinstance(body, dict) else None


async def _load_prior_state(session: AsyncSession) -> tuple[set[str], dict[str, KnowledgeArtifact]]:
    """(all live alias_reference titles — any provenance, for collision detection,
    dict[title -> row] of ONLY the ones this pass owns (provenance='ledger_mined'),
    for this pass's own upsert/invalidate scope)."""
    rows = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(
                    KnowledgeArtifact.artifact_type == ALIAS_ARTIFACT_TYPE,
                    KnowledgeArtifact.invalidated_at_seq.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    all_titles: set[str] = set()
    ledger_rows: dict[str, KnowledgeArtifact] = {}
    for row in rows:
        if row.title is None:
            continue
        all_titles.add(row.title)
        body = _parse_body(row)
        if body is not None and body.get("provenance") == LEDGER_MINED_PROVENANCE:
            ledger_rows[row.title] = row
    return all_titles, ledger_rows


def _desired_body(group: _MissGroup, *, target_path: str, artifact_id: uuid.UUID) -> str:
    body = {
        "schema": ALIAS_BODY_SCHEMA,
        "alias": group.phrase,
        "variants": list(phrase_variants(group.phrase)),
        "confidence_tier": "interpreted",
        "confirmation_count": group.confirmation_count,
        "provenance": LEDGER_MINED_PROVENANCE,
        "targets": [{"path": target_path, "artifact_id": str(artifact_id), "count": 1}],
        # List-wrapped: alias/run.py's _prior_extractions iterates `evidence`
        # generically for EVERY live alias_reference row (any provenance) — a bare
        # object here would break that iteration (alias-reference.md).
        "evidence": [
            {
                "first_seen": group.first_seen.astimezone(UTC).isoformat(),
                "last_seen": group.last_seen.astimezone(UTC).isoformat(),
                "miss_count": group.miss_count,
            }
        ],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def run_ledger_alias_miner(
    session: AsyncSession,
    *,
    kb_version: str,
    valid_from_seq: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> LedgerMiningResult:
    """Execute the ledger-mining reconcile for this build. Flushes, never commits
    — the build runner owns the transaction (same policy as every other finalize
    step)."""
    window_start = datetime.now(UTC) - timedelta(days=window_days)
    groups, misses_seen = await _load_miss_groups(session, window_start=window_start)
    candidates = await _load_candidates(session)
    all_titles, ledger_rows = await _load_prior_state(session)

    mined = already_aliased = unresolved = 0
    inserted = refreshed = unchanged = 0
    desired_titles: set[str] = set()
    for group in groups:
        resolution = resolve(group.phrase, candidates.entries)
        if resolution is None:
            unresolved += 1
            logger.info(
                "event=ledger_mining_unresolved phrase=%r miss_count=%d",
                group.phrase,
                group.miss_count,
            )
            continue
        target_path = resolution.targets[0]
        artifact_id = candidates.path_artifact.get(target_path)
        if artifact_id is None:
            # Defensive: the winning entry's path has no resolvable live artifact.
            # Unreachable in practice (every candidate entry is built FROM a live
            # artifact's own path) — surface it rather than silently dropping.
            logger.warning(
                "event=ledger_mining_target_unresolved phrase=%r path=%s",
                group.phrase,
                target_path,
            )
            unresolved += 1
            continue
        mined += 1
        if group.phrase in all_titles and group.phrase not in ledger_rows:
            # Already resolved by a DIFFERENT provenance (e.g. a commit/doc mined
            # the exact same phrase) — never create a duplicate title.
            already_aliased += 1
            logger.info("event=ledger_mining_already_aliased phrase=%r", group.phrase)
            continue
        body_text = _desired_body(group, target_path=target_path, artifact_id=artifact_id)
        acl_teams = commit_acl_intersection(
            [target_path], {target_path: candidates.path_acl[target_path]}
        )
        search_text = " ".join((group.phrase, *phrase_variants(group.phrase)))
        anchor_source_id = candidates.path_source[target_path]
        desired_titles.add(group.phrase)
        row = ledger_rows.get(group.phrase)
        if row is None:
            session.add(
                KnowledgeArtifact(
                    artifact_type=ALIAS_ARTIFACT_TYPE,
                    source_id=anchor_source_id,
                    title=group.phrase,
                    body_text=body_text,
                    content_hash=content_hash(body_text),
                    search_text=search_text,
                    kb_version=kb_version,
                    valid_from_seq=valid_from_seq,
                    knowledge_kind="interpreted",
                    acl_teams=acl_teams,
                )
            )
            inserted += 1
            continue
        same = (
            row.body_text == body_text
            and row.search_text == search_text
            and list(row.acl_teams) == acl_teams
            and row.source_id == anchor_source_id
            and row.invalidated_at_seq is None
        )
        if same:
            unchanged += 1
            continue
        # In-place refresh (linker-edge / PR-38 precedent): keep the original
        # valid_from_seq.
        row.body_text = body_text
        row.content_hash = content_hash(body_text)
        row.search_text = search_text
        row.acl_teams = acl_teams
        row.source_id = anchor_source_id
        row.kb_version = kb_version
        refreshed += 1

    invalidated = 0
    for title, row in sorted(ledger_rows.items()):
        if title in desired_titles:
            continue
        row.invalidated_at_seq = valid_from_seq
        invalidated += 1
        logger.info(
            "event=ledger_mining_invalidated reason=no_longer_mined title=%r artifact_id=%s "
            "invalidated_at_seq=%d",
            title,
            row.artifact_id,
            valid_from_seq,
        )
    await session.flush()

    result = LedgerMiningResult(
        misses_seen=misses_seen,
        phrases_seen=len(groups),
        mined=mined,
        already_aliased=already_aliased,
        unresolved=unresolved,
        artifacts_inserted=inserted,
        artifacts_refreshed=refreshed,
        artifacts_unchanged=unchanged,
        artifacts_invalidated=invalidated,
    )
    logger.info(
        "event=ledger_mining_completed kb_version=%s build_seq=%d window_days=%d misses_seen=%d "
        "phrases_seen=%d mined=%d already_aliased=%d unresolved=%d artifacts_inserted=%d "
        "artifacts_refreshed=%d artifacts_unchanged=%d artifacts_invalidated=%d",
        kb_version,
        valid_from_seq,
        window_days,
        result.misses_seen,
        result.phrases_seen,
        result.mined,
        result.already_aliased,
        result.unresolved,
        result.artifacts_inserted,
        result.artifacts_refreshed,
        result.artifacts_unchanged,
        result.artifacts_invalidated,
    )
    return result


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MAX_RAW_QUERY_CHARS",
    "LedgerMiningResult",
    "run_ledger_alias_miner",
]
