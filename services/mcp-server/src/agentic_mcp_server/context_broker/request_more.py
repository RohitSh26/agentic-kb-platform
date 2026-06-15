"""context.request_more: justified follow-up retrieval with dedupe-before-budget.

Outcome order is contractual (docs/contracts/mcp-tools-contract.md): exact
reuse, then semantic reuse — both free — then per-agent denial, then per-run
needs_human_approval, and only then a charged retrieval. Dedupe runs before
budget denial so an agent is never charged (or refused) for evidence the run
already paid for.
"""

import logging
import time
import uuid
from typing import Literal

from fastmcp.exceptions import ToolError

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.error_ledger import write_error_event
from agentic_mcp_server.context_broker.retrieval import (
    authorization_decision,
    card_tokens,
    retrieve_cards,
)
from agentic_mcp_server.context_broker.state import EvidencePackState, UnknownPackError
from agentic_mcp_server.domain.query_text import normalize_query
from agentic_mcp_server.infrastructure.postgres.artifacts import fetch_artifacts
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.mcp.tool_schemas.context import RequestMoreRequest, RequestMoreResponse
from agentic_mcp_server.mcp.tool_schemas.evidence import EvidenceCard
from agentic_mcp_server.telemetry.audit import audit_context_access

logger = logging.getLogger(__name__)

_TOOL_NAME = "context.request_more"


async def request_more(
    deps: BrokerDeps, request: RequestMoreRequest, requester: Requester
) -> RequestMoreResponse:
    started = time.monotonic()
    try:
        pack = deps.packs.get(request.context_pack_id)
    except UnknownPackError:
        await write_error_event(
            deps,
            tool_name=_TOOL_NAME,
            subject=requester.subject,
            query_text=request.context_pack_id,
        )
        raise ToolError(f"unknown context_pack_id: {request.context_pack_id}") from None

    normalized = normalize_query(request.question)

    async def write_ledger(
        status: str,
        *,
        reused: list[str] | None = None,
        new: list[str] | None = None,
        cache_hit: bool = False,
        semantic_reuse: bool = False,
        tokens_returned: int = 0,
    ) -> None:
        reused = reused or []
        new = new or []
        async with deps.session_factory() as session:
            await insert_event(
                session,
                RetrievalEventInsert(
                    run_id=pack.run_id,
                    agent_name=requester.subject,
                    tool_name=_TOOL_NAME,
                    status=status,
                    kb_version=pack.kb_version,
                    context_pack_id=uuid.UUID(pack.context_pack_id),
                    query_text=request.question,
                    normalized_query=normalized,
                    retrieval_profile=pack.retrieval_profile,
                    returned_artifact_ids=[uuid.UUID(e) for e in [*reused, *new]],
                    reused_evidence_ids=[uuid.UUID(e) for e in reused],
                    new_evidence_ids=[uuid.UUID(e) for e in new],
                    cache_hit=cache_hit,
                    semantic_reuse=semantic_reuse,
                    tokens_returned=tokens_returned,
                    latency_ms=int((time.monotonic() - started) * 1000),
                ),
            )
        logger.info(
            "broker.request_more context_pack_id=%s subject=%s status=%s "
            "cache_hit=%s semantic_reuse=%s tokens=%d",
            pack.context_pack_id,
            requester.subject,
            status,
            cache_hit,
            semantic_reuse,
            tokens_returned,
        )

    async with pack.lock:
        # reuse crosses requesters within a run, so reused ids are re-filtered
        # for the caller; if the ACL suppresses every id, fall through to a
        # fresh (already-filtered) retrieval instead of leaking their existence
        exact = pack.history.find_exact(normalized)
        if exact is not None:
            reused = list(exact.evidence_ids)
            allowed = await _authorized_reuse(deps, pack, requester, reused)
            if allowed or not reused:
                await write_ledger("reused", reused=allowed, cache_hit=True)
                return _reuse_response(deps, pack, allowed)

        semantic = pack.history.find_semantic(normalized, deps.settings.semantic_reuse_threshold)
        if semantic is not None:
            reused = list(semantic.evidence_ids)
            allowed = await _authorized_reuse(deps, pack, requester, reused)
            if allowed or not reused:
                await write_ledger("reused", reused=allowed, semantic_reuse=True)
                return _reuse_response(deps, pack, allowed)

        allowance = deps.budget_policy.allowance_for(requester.subject)
        usage = pack.usage_for(requester.subject)
        if usage.requests + 1 > allowance.max_requests:
            reason = (
                f"agent request allowance exhausted: {usage.requests}/{allowance.max_requests} "
                "requests used"
            )
            await write_ledger("denied")
            return _refusal_response(deps, pack, "denied", reason)
        if usage.tokens + request.max_tokens > allowance.max_tokens:
            reason = (
                f"agent token allowance exceeded: {usage.tokens} used + {request.max_tokens} "
                f"requested > {allowance.max_tokens} allowed"
            )
            await write_ledger("denied")
            return _refusal_response(deps, pack, "denied", reason)

        if request.max_tokens > pack.run_remaining_tokens:
            await write_ledger("needs_human_approval")
            return _refusal_response(deps, pack, "needs_human_approval", None)

        # A follow-up request is intent-neutral by design (no intent forwarded), so
        # temporal weighting stays off here — a request_more is a targeted top-up for
        # a specific question, not an intent-shaped initial pack.
        cards, _ = await retrieve_cards(
            deps,
            query=request.question,
            kb_version=pack.kb_version,
            build_seq=pack.build_seq,
            requester=requester,
            tool=_TOOL_NAME,
        )
        excluded = set(request.already_checked_evidence_ids) | set(pack.cards)
        new_cards: list[EvidenceCard] = []
        tokens = 0
        for card in cards:
            if card.evidence_id in excluded:
                continue
            cost = card_tokens(card)
            if tokens + cost > request.max_tokens:
                break
            new_cards.append(card)
            tokens += cost

        pack.charge(requester.subject, tokens)
        usage.requests += 1
        for card in new_cards:
            pack.cards[card.evidence_id] = card
        new_ids = [card.evidence_id for card in new_cards]
        pack.history.record(normalized, new_ids)

        await write_ledger("approved", new=new_ids, tokens_returned=tokens)
    return RequestMoreResponse(
        status="approved",
        reused_evidence_ids=[],
        new_evidence_cards=new_cards,
        tokens_returned=tokens,
        budget_remaining_tokens=pack.run_remaining_tokens,
        authorization=authorization_decision(deps),
    )


async def _authorized_reuse(
    deps: BrokerDeps, pack: EvidencePackState, requester: Requester, reused: list[str]
) -> list[str]:
    if not reused:
        return []
    async with deps.session_factory() as session:
        artifacts = await fetch_artifacts(
            session, [uuid.UUID(evidence_id) for evidence_id in reused], pack.build_seq
        )
    allowed = {
        str(artifact.artifact_id)
        for artifact in deps.authorization.filter_artifacts(requester, artifacts)
    }
    audit_context_access(
        tool=_TOOL_NAME,
        requester=requester,
        kb_version=pack.kb_version,
        artifact_ids=[uuid.UUID(e) for e in reused if e in allowed],
        suppressed_artifact_ids=[uuid.UUID(e) for e in reused if e not in allowed],
    )
    return [evidence_id for evidence_id in reused if evidence_id in allowed]


def _reuse_response(
    deps: BrokerDeps, pack: EvidencePackState, reused: list[str]
) -> RequestMoreResponse:
    return RequestMoreResponse(
        status="reused",
        reused_evidence_ids=reused,
        new_evidence_cards=[],
        tokens_returned=0,
        budget_remaining_tokens=pack.run_remaining_tokens,
        authorization=authorization_decision(deps),
    )


def _refusal_response(
    deps: BrokerDeps,
    pack: EvidencePackState,
    status: Literal["denied", "needs_human_approval"],
    denial_reason: str | None,
) -> RequestMoreResponse:
    return RequestMoreResponse(
        status=status,
        reused_evidence_ids=[],
        new_evidence_cards=[],
        tokens_returned=0,
        budget_remaining_tokens=pack.run_remaining_tokens,
        authorization=authorization_decision(deps),
        denial_reason=denial_reason,
    )
