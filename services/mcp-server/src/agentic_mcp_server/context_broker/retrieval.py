"""Shared retrieval path: search hints -> Postgres hydration -> ACL -> ranked cards.

Search results are relevance hints only; every card is hydrated from Postgres,
the source of truth, and filtered through the authorization policy before
anything reaches an agent (invariant 1, invariant 6). Every pass through this
path is audit-logged with the ids the ACL suppressed.
"""

import logging
import uuid

from agentic_mcp_server.auth.rbac import Requester
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


def _rank_key(
    artifact: ArtifactRow,
    scores: dict[uuid.UUID, float],
    temporal: dict[uuid.UUID, TemporalWeight],
) -> tuple[int, float, float, str]:
    """Deterministic rank key. The temporal weight multiplies the search score (a
    TRANSPARENT, logged factor — never a hidden reranker); the source_backed and
    authority tiers and the artifact_id tie-break are unchanged so ordering stays
    stable for equal inputs. A neutral (intent=None) weight is 1.0 ⇒ identical
    ordering to the pre-PR-33 ranker."""
    source_backed = 1 if artifact.knowledge_kind == "source_backed" else 0
    authority = artifact.authority_score or 0.0
    base_score = scores.get(artifact.artifact_id, 0.0)
    weight = temporal[artifact.artifact_id].weight if artifact.artifact_id in temporal else 1.0
    return (source_backed, authority, base_score * weight, str(artifact.artifact_id))


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


def card_tokens(card: EvidenceCard) -> int:
    return estimate_tokens(card.title) + estimate_tokens(card.summary)


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

    ranked = sorted(allowed, key=lambda a: _rank_key(a, scores, temporal), reverse=True)[:top]
    cards = [build_card(artifact, temporal.get(artifact.artifact_id)) for artifact in ranked]
    logger.info(
        "event=temporal_weight_summary tool=%s intent=%s candidates=%d ranked=%d "
        "stale_docs=%d order=%s",
        tool,
        intent or "none",
        len(allowed),
        len(ranked),
        stale_count,
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
