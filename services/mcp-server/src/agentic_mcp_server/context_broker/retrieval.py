"""Shared retrieval path: search hints -> Postgres hydration -> ACL -> ranked cards.

Search results are relevance hints only; every card is hydrated from Postgres,
the source of truth, and filtered through the authorization policy before
anything reaches an agent (invariant 1, invariant 6). Every pass through this
path is audit-logged with the ids the ACL suppressed.
"""

import logging
import uuid

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dedupe import similarity
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.temporal import (
    Intent,
    TemporalSignals,
    TemporalWeight,
    compute_weight,
    derive_source_kind,
    derive_state,
    is_stale_doc_for_intent,
)
from agentic_mcp_server.context_broker.untrusted import scan_for_injection
from agentic_mcp_server.domain.query_text import normalize_query
from agentic_mcp_server.domain.token_budget import estimate_tokens
from agentic_mcp_server.infrastructure.postgres.artifacts import (
    ArtifactRow,
    fetch_artifacts,
    fetch_current_symbol_titles,
)
from agentic_mcp_server.mcp.tool_schemas.evidence import AuthorizationDecision, EvidenceCard
from agentic_mcp_server.telemetry.audit import audit_context_access

logger = logging.getLogger(__name__)

# search wider than the card cap so ACL filtering and rerank have slack
_SEARCH_OVERSAMPLE = 4


def _temporal_signals(artifact: ArtifactRow) -> TemporalSignals:
    return TemporalSignals(
        source_type=artifact.source_type,
        artifact_type=artifact.artifact_type,
        invalidated_at_seq=artifact.invalidated_at_seq,
        source_is_deleted=artifact.source_is_deleted,
    )


# ADR-0028: graph-centrality prior weight. A small multiplicative lift on the relevance term so a
# structurally-central node outranks an equally-keyword-matching leaf, WITHOUT overriding the
# provenance (source_backed) or authority tiers. NULL/0 centrality gives a 1.0 factor (no change).
_CENTRALITY_BETA = 0.25


def _rank_key(
    artifact: ArtifactRow,
    scores: dict[uuid.UUID, float],
    temporal: dict[uuid.UUID, TemporalWeight],
) -> tuple[int, float, float, str]:
    """Deterministic rank key. The temporal weight and the centrality prior multiply the search
    score (TRANSPARENT, logged factors — never a hidden reranker); the source_backed and authority
    tiers and the artifact_id tie-break are unchanged so ordering stays stable for equal inputs. A
    neutral temporal weight (intent=None) is 1.0 and a NULL/0 centrality is a 1.0 factor, so the
    ordering is identical to the pre-PR-33/PR-36 ranker."""
    source_backed = 1 if artifact.knowledge_kind == "source_backed" else 0
    authority = artifact.authority_score or 0.0
    base_score = scores.get(artifact.artifact_id, 0.0)
    weight = temporal[artifact.artifact_id].weight if artifact.artifact_id in temporal else 1.0
    centrality = artifact.centrality_score or 0.0
    relevance = base_score * weight * (1.0 + _CENTRALITY_BETA * centrality)
    return (source_backed, authority, relevance, str(artifact.artifact_id))


def build_card(artifact: ArtifactRow, temporal: TemporalWeight | None = None) -> EvidenceCard:
    body = artifact.body_text or ""
    title = artifact.title or str(artifact.artifact_id)
    summary = _card_summary(body)
    scan = scan_for_injection(title, summary)
    if temporal is None:
        source_kind = derive_source_kind(artifact.source_type, artifact.artifact_type)
        temporal_state = derive_state(_temporal_signals(artifact))
        stale_for_intent = False
    else:
        source_kind = temporal.source_kind
        temporal_state = temporal.state
        stale_for_intent = temporal.stale_for_intent
    return EvidenceCard(
        evidence_id=str(artifact.artifact_id),
        artifact_id=artifact.artifact_id,
        level="L1",
        card_type=artifact.artifact_type,
        title=title,
        summary=summary,
        source_uri=artifact.source_uri,
        display_citation=display_citation(artifact),
        confidence=1.0 if artifact.knowledge_kind == "source_backed" else 0.5,
        authority_score=min(max(artifact.authority_score or 0.0, 0.0), 1.0),
        tokens_if_expanded=estimate_tokens(body),
        injection_flagged=scan.flagged,
        injection_signals=list(scan.signals),
        source_kind=source_kind,
        temporal_state=temporal_state,
        stale_for_intent=stale_for_intent,
    )


def _card_summary(body: str, max_chars: int = 280) -> str:
    first_line = next(iter(body.strip().splitlines()), "")
    return first_line[:max_chars]


def readable_path(source_uri: str) -> str:
    """Best-effort repo-relative path from a source_uri, for a human citation.

    github://owner/repo/<path> -> <path>; other schemes fall back to the part after '://'.
    """
    rest = source_uri.split("://", 1)[1] if "://" in source_uri else source_uri
    if source_uri.startswith("github://"):
        parts = rest.split("/", 2)  # owner, repo, path
        return parts[2] if len(parts) == 3 else rest
    return rest


def display_citation(artifact: ArtifactRow) -> str:
    """Human-readable citation (``file:symbol``) from artifact metadata (ADR-0022).

    This is the user-facing reference; it is distinct from ``evidence_id`` (the UUID audit
    handle) so a model never needs to surface a UUID in prose. Symbol-shaped artifacts get
    ``path:symbol``; files/docs get the path alone.
    """
    path = readable_path(artifact.source_uri)
    title = (artifact.title or "").strip()
    if artifact.artifact_type in ("code_symbol", "endpoint") and title:
        return f"{path}:{title}" if path else title
    return path or title or str(artifact.artifact_id)


def _dedupe_text(artifact: ArtifactRow) -> str:
    """Normalized title + summary used as the within-retrieval dedupe signal.

    Card title/summary are what an agent actually sees, so collapsing on them
    (not the full body) matches the contract's "semantic dedupe before the card
    cap" — two artifacts that surface as the same card cost a card slot for no
    new evidence.
    """
    title = artifact.title or str(artifact.artifact_id)
    return normalize_query(f"{title} {_card_summary(artifact.body_text or '')}")


def _collapse_near_duplicates(
    ranked: list[ArtifactRow], threshold: float
) -> tuple[list[ArtifactRow], list[tuple[uuid.UUID, uuid.UUID]]]:
    """Drop near-duplicate candidates, keeping the higher-ranked of each pair.

    `ranked` is already in deterministic rank order (best first); we keep a
    candidate only if it is not a near-duplicate of an already-kept one, so the
    survivor is always the better-ranked card and the result stays deterministic.
    Returns the survivors plus (dropped_id, kept_id) pairs for logging.
    """
    kept: list[ArtifactRow] = []
    kept_text: list[str] = []
    dropped: list[tuple[uuid.UUID, uuid.UUID]] = []
    for candidate in ranked:
        text = _dedupe_text(candidate)
        duplicate_of: ArtifactRow | None = None
        for keeper, keeper_text in zip(kept, kept_text, strict=True):
            if similarity(text, keeper_text) >= threshold:
                duplicate_of = keeper
                break
        if duplicate_of is None:
            kept.append(candidate)
            kept_text.append(text)
        else:
            dropped.append((candidate.artifact_id, duplicate_of.artifact_id))
    return kept, dropped


def card_tokens(card: EvidenceCard) -> int:
    """Tokens the agent actually pays to ingest this card.

    A card is delivered as its full JSON serialization — two UUIDs, the source_uri,
    and ~10 fixed scalar/enum fields, not just title+summary. Charging only a few
    hand-picked fields under-counted the wire cost ~3.3x, so the budget meter let an
    agent ingest far more than its allowance thought. Estimate against the EXACT
    serialized payload so the meter == what crosses the wire (verified against a live
    run: 33.6 KB / 30 cards ~ 280 tok/card, which this now charges).
    """
    return estimate_tokens(card.model_dump_json())


def authorization_decision(deps: BrokerDeps) -> AuthorizationDecision:
    return AuthorizationDecision(policy=deps.authorization.policy_name)


async def retrieve_cards(
    deps: BrokerDeps,
    *,
    query: str,
    kb_version: str,
    build_seq: int,
    requester: Requester,
    tool: str,
    intent: Intent | None = None,
) -> tuple[list[EvidenceCard], list[ArtifactRow]]:
    """Run the full retrieval path and return ranked cards plus their artifacts.

    Both the search hints and the Postgres hydration are scoped by interval
    membership against `build_seq` (version-membership.md); `kb_version` is the
    label carried into the audit log only.

    When `intent` is supplied (PR-33), evidence is re-weighted by the temporal
    semantics for that intent — current code lifted for `how`/structure queries,
    cards/PRs/ADRs lifted for `why`, superseded + stale docs downranked. The
    weighting is TRANSPARENT and LOGGED (event=temporal_weight*), deterministic,
    and never touches membership, ACL, or the L0 verifier.
    """
    top = deps.settings.max_cards_per_retrieval
    hits = await deps.search_client.search(query, build_seq=build_seq, top=top * _SEARCH_OVERSAMPLE)
    if not hits:
        return [], []
    scores = {hit.artifact_id: hit.score for hit in hits}
    async with deps.session_factory() as session:
        artifacts = await fetch_artifacts(session, list(scores), build_seq)
        # Only fetch the current-symbol reference set when an intent can use the
        # stale-doc signal (avoids an extra read on the neutral path).
        current_symbols: frozenset[str] = frozenset()
        if intent is not None:
            current_symbols = frozenset(await fetch_current_symbol_titles(session, build_seq))
    allowed = deps.authorization.filter_artifacts(requester, artifacts)
    allowed_ids = {artifact.artifact_id for artifact in allowed}

    # Deterministic temporal weight per candidate (logged inside compute_weight).
    temporal: dict[uuid.UUID, TemporalWeight] = {}
    stale_count = 0
    for artifact in allowed:
        kind = derive_source_kind(artifact.source_type, artifact.artifact_type)
        stale = is_stale_doc_for_intent(
            intent=intent,
            source_kind=kind,
            body_text=artifact.body_text,
            title=artifact.title,
            current_symbols=current_symbols,
        )
        if stale:
            stale_count += 1
        temporal[artifact.artifact_id] = compute_weight(
            artifact_id=artifact.artifact_id,
            intent=intent,
            signals=_temporal_signals(artifact),
            stale_for_intent=stale,
        )

    # Rank the FULL candidate set, then collapse near-duplicates BEFORE the card
    # cap so two artifacts that surface as the same card do not each consume a
    # slot (token-budgets rule: semantic dedupe, then 3-5 cards max). Dedupe runs
    # on the already-sorted list so the higher-ranked of any duplicate pair wins
    # and the outcome is deterministic.
    ranked_all = sorted(allowed, key=lambda a: _rank_key(a, scores, temporal), reverse=True)
    deduped, dropped = _collapse_near_duplicates(ranked_all, deps.settings.semantic_dupe_threshold)
    if dropped:
        logger.info(
            "event=retrieval_deduped tool=%s threshold=%.2f dropped=%d pairs=%s",
            tool,
            deps.settings.semantic_dupe_threshold,
            len(dropped),
            ",".join(f"{dropped_id}->{kept_id}" for dropped_id, kept_id in dropped),
        )
    ranked = deduped[:top]
    cards = [build_card(artifact, temporal.get(artifact.artifact_id)) for artifact in ranked]
    logger.info(
        "event=temporal_weight_summary tool=%s intent=%s candidates=%d ranked=%d "
        "stale_docs=%d centrality_lifted=%d order=%s",
        tool,
        intent or "none",
        len(allowed),
        len(ranked),
        stale_count,
        # ranked artifacts carrying a non-zero centrality prior (ADR-0028, transparent factor)
        sum(1 for a in ranked if (a.centrality_score or 0.0) > 0.0),
        ",".join(str(card.artifact_id) for card in cards),
    )
    audit_context_access(
        tool=tool,
        requester=requester,
        kb_version=kb_version,
        artifact_ids=[card.artifact_id for card in cards],
        suppressed_artifact_ids=[
            artifact.artifact_id
            for artifact in artifacts
            if artifact.artifact_id not in allowed_ids
        ],
        injection_flagged_ids=[card.artifact_id for card in cards if card.injection_flagged],
    )
    return cards, ranked
