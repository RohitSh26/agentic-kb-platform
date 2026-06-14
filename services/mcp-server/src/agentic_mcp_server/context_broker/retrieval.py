"""Shared retrieval path: search hints -> Postgres hydration -> ACL -> ranked cards.

Search results are relevance hints only; every card is hydrated from Postgres,
the source of truth, and filtered through the authorization policy before
anything reaches an agent (invariant 1, invariant 6). Every pass through this
path is audit-logged with the ids the ACL suppressed.
"""

import uuid

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.untrusted import scan_for_injection
from agentic_mcp_server.domain.token_budget import estimate_tokens
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow, fetch_artifacts
from agentic_mcp_server.mcp.tool_schemas.evidence import AuthorizationDecision, EvidenceCard
from agentic_mcp_server.telemetry.audit import audit_context_access

# search wider than the card cap so ACL filtering and rerank have slack
_SEARCH_OVERSAMPLE = 4


def _rank_key(
    artifact: ArtifactRow, scores: dict[uuid.UUID, float]
) -> tuple[int, float, float, str]:
    source_backed = 1 if artifact.knowledge_kind == "source_backed" else 0
    authority = artifact.authority_score or 0.0
    score = scores.get(artifact.artifact_id, 0.0)
    return (source_backed, authority, score, str(artifact.artifact_id))


def build_card(artifact: ArtifactRow) -> EvidenceCard:
    body = artifact.body_text or ""
    title = artifact.title or str(artifact.artifact_id)
    summary = _card_summary(body)
    scan = scan_for_injection(title, summary)
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
    )


def _card_summary(body: str, max_chars: int = 280) -> str:
    first_line = next(iter(body.strip().splitlines()), "")
    return first_line[:max_chars]


def card_tokens(card: EvidenceCard) -> int:
    return estimate_tokens(card.title) + estimate_tokens(card.summary)


def authorization_decision(deps: BrokerDeps) -> AuthorizationDecision:
    return AuthorizationDecision(policy=deps.authorization.policy_name)


async def retrieve_cards(
    deps: BrokerDeps, *, query: str, kb_version: str, requester: Requester, tool: str
) -> tuple[list[EvidenceCard], list[ArtifactRow]]:
    """Run the full retrieval path and return ranked cards plus their artifacts."""
    top = deps.settings.max_cards_per_retrieval
    hits = await deps.search_client.search(
        query, kb_version=kb_version, top=top * _SEARCH_OVERSAMPLE
    )
    if not hits:
        return [], []
    scores = {hit.artifact_id: hit.score for hit in hits}
    async with deps.session_factory() as session:
        artifacts = await fetch_artifacts(session, list(scores), kb_version)
    allowed = deps.authorization.filter_artifacts(requester, artifacts)
    allowed_ids = {artifact.artifact_id for artifact in allowed}
    ranked = sorted(allowed, key=lambda a: _rank_key(a, scores), reverse=True)[:top]
    cards = [build_card(artifact) for artifact in ranked]
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
