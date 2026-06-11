"""context.create_pack and context.read_pack.

The pack is built once per run from the approved context plan (retrieve once,
reuse aggressively — invariant 3). read_pack is free: it serves the existing
cards and charges nothing.
"""

import logging
import time
import uuid

from fastmcp.exceptions import ToolError

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.error_ledger import write_error_event
from agentic_mcp_server.context_broker.retrieval import (
    authorization_decision,
    card_tokens,
    retrieve_cards,
)
from agentic_mcp_server.context_broker.state import (
    EvidencePackState,
    UnknownPackError,
    new_pack_id,
)
from agentic_mcp_server.domain.query_text import normalize_query
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_kb_version
from agentic_mcp_server.infrastructure.postgres.artifacts import fetch_artifacts
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.mcp.tool_schemas.context import (
    CreatePackRequest,
    CreatePackResponse,
    ReadPackRequest,
    ReadPackResponse,
)
from agentic_mcp_server.mcp.tool_schemas.evidence import EvidenceCard
from agentic_mcp_server.telemetry.audit import audit_context_access

logger = logging.getLogger(__name__)


def _summary(run_id: str, cards: list[EvidenceCard]) -> str:
    if not cards:
        return f"Evidence pack for run {run_id}: no evidence found."
    titles = "; ".join(card.title for card in cards)
    return f"Evidence pack for run {run_id}: {len(cards)} cards covering {titles}."


async def create_pack(
    deps: BrokerDeps, request: CreatePackRequest, requester: Requester
) -> CreatePackResponse:
    started = time.monotonic()
    query = f"{request.task} {request.approved_context_plan}"
    async with deps.session_factory() as session:
        kb_version = await fetch_active_kb_version(session)
    if kb_version is None:
        await write_error_event(
            deps,
            tool_name="context.create_pack",
            subject=requester.subject,
            run_id=request.run_id,
            query_text=query,
        )
        raise ToolError("no active kb_version; the knowledge base has not been built yet")

    cards, _ = await retrieve_cards(
        deps,
        query=query,
        kb_version=kb_version,
        requester=requester,
        tool="context.create_pack",
    )
    used_tokens = sum(card_tokens(card) for card in cards)
    open_questions = [] if cards else [f"No evidence found for: {request.task}"]

    pack = EvidencePackState(
        context_pack_id=new_pack_id(),
        run_id=request.run_id,
        kb_version=kb_version,
        retrieval_profile=request.retrieval_profile,
        summary=_summary(request.run_id, cards),
        budget_tokens=request.budget_tokens,
        used_run_tokens=used_tokens,
        cards={card.evidence_id: card for card in cards},
        open_questions=open_questions,
    )
    normalized = normalize_query(query)
    pack.history.record(normalized, [card.evidence_id for card in cards])
    deps.packs.create(pack)

    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=request.run_id,
                agent_name=requester.subject,
                tool_name="context.create_pack",
                status="approved",
                kb_version=kb_version,
                context_pack_id=uuid.UUID(pack.context_pack_id),
                query_text=query,
                normalized_query=normalized,
                retrieval_profile=request.retrieval_profile,
                returned_artifact_ids=[card.artifact_id for card in cards],
                new_evidence_ids=[card.artifact_id for card in cards],
                tokens_returned=used_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )
    logger.info(
        "broker.create_pack run_id=%s context_pack_id=%s subject=%s cards=%d tokens=%d",
        request.run_id,
        pack.context_pack_id,
        requester.subject,
        len(cards),
        used_tokens,
    )
    return CreatePackResponse(
        context_pack_id=pack.context_pack_id,
        kb_version=kb_version,
        summary=pack.summary,
        evidence_cards=cards,
        open_questions=pack.open_questions,
        budget_used_tokens=used_tokens,
        authorization=authorization_decision(deps),
    )


async def read_pack(
    deps: BrokerDeps, request: ReadPackRequest, requester: Requester
) -> ReadPackResponse:
    started = time.monotonic()
    try:
        pack = deps.packs.get(request.context_pack_id)
    except UnknownPackError:
        await write_error_event(
            deps,
            tool_name="context.read_pack",
            subject=requester.subject,
            query_text=request.context_pack_id,
        )
        raise ToolError(f"unknown context_pack_id: {request.context_pack_id}") from None

    # a pack handle is not a grant: cards were filtered against the creator's
    # teams, so re-apply the ACL for the reading requester before serving them
    all_cards = list(pack.cards.values())
    async with deps.session_factory() as session:
        artifacts = await fetch_artifacts(
            session, [card.artifact_id for card in all_cards], pack.kb_version
        )
    allowed_ids = {
        artifact.artifact_id
        for artifact in deps.authorization.filter_artifacts(requester, artifacts)
    }
    cards = [card for card in all_cards if card.artifact_id in allowed_ids]
    audit_context_access(
        tool="context.read_pack",
        requester=requester,
        kb_version=pack.kb_version,
        artifact_ids=[card.artifact_id for card in cards],
        suppressed_artifact_ids=[
            card.artifact_id for card in all_cards if card.artifact_id not in allowed_ids
        ],
    )
    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=pack.run_id,
                agent_name=requester.subject,
                tool_name="context.read_pack",
                status="reused",
                kb_version=pack.kb_version,
                context_pack_id=uuid.UUID(pack.context_pack_id),
                retrieval_profile=pack.retrieval_profile,
                reused_evidence_ids=[card.artifact_id for card in cards],
                cache_hit=True,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )
    logger.info(
        "broker.read_pack context_pack_id=%s subject=%s role=%s cards=%d",
        pack.context_pack_id,
        requester.subject,
        request.role,
        len(cards),
    )
    return ReadPackResponse(
        context_pack_id=pack.context_pack_id,
        kb_version=pack.kb_version,
        role=request.role,
        # recomputed from the filtered cards: the stored summary names every
        # title the creator could see
        summary=_summary(pack.run_id, cards),
        evidence_cards=cards,
        open_questions=pack.open_questions,
        budget_remaining_tokens=pack.run_remaining_tokens,
        authorization=authorization_decision(deps),
    )
