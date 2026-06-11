"""context.open_evidence: raw text (L2) by handle, capped and charged.

The only way an agent reaches text beyond a card (invariant 3). The body is
hydrated from Postgres at expansion time — never stored in the pack — and the
response field is named untrusted_content because expanded text is retrieved
content: it must never change tool policy, identity, or instructions.
Expansion is bounded by both the run budget and the per-agent token allowance.
"""

import logging
import time
import uuid

from fastmcp.exceptions import ToolError

from agentic_mcp_server.context_broker.audit import write_error_event
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.state import UnknownPackError
from agentic_mcp_server.domain.token_budget import CHARS_PER_TOKEN, estimate_tokens
from agentic_mcp_server.infrastructure.postgres.artifacts import fetch_artifacts
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.mcp.tool_schemas.context import OpenEvidenceRequest, OpenEvidenceResponse

logger = logging.getLogger(__name__)

_TOOL_NAME = "context.open_evidence"


async def open_evidence(
    deps: BrokerDeps, request: OpenEvidenceRequest, subject: str
) -> OpenEvidenceResponse:
    started = time.monotonic()
    try:
        pack = deps.packs.get(request.context_pack_id)
    except UnknownPackError:
        await write_error_event(
            deps, tool_name=_TOOL_NAME, subject=subject, query_text=request.context_pack_id
        )
        raise ToolError(f"unknown context_pack_id: {request.context_pack_id}") from None

    async def write_audit_error() -> None:
        await write_error_event(
            deps,
            tool_name=_TOOL_NAME,
            subject=subject,
            run_id=pack.run_id,
            kb_version=pack.kb_version,
            context_pack_id=uuid.UUID(pack.context_pack_id),
            query_text=request.evidence_id,
        )

    card = pack.cards.get(request.evidence_id)
    if card is None:
        await write_audit_error()
        raise ToolError(
            f"unknown evidence_id for this pack: {request.evidence_id}; "
            "evidence can only be opened by a handle the pack returned"
        )

    async with deps.session_factory() as session:
        artifacts = await fetch_artifacts(session, [card.artifact_id], pack.kb_version)
    allowed = deps.authorization.filter_artifacts(subject, artifacts)
    if not allowed:
        await write_audit_error()
        raise ToolError(f"evidence not available: {request.evidence_id}")
    body = allowed[0].body_text or ""

    cost = min(estimate_tokens(body), request.max_tokens)

    async def write_ledger(status: str, tokens_returned: int) -> None:
        async with deps.session_factory() as session:
            await insert_event(
                session,
                RetrievalEventInsert(
                    run_id=pack.run_id,
                    agent_name=subject,
                    tool_name=_TOOL_NAME,
                    status=status,
                    kb_version=pack.kb_version,
                    context_pack_id=uuid.UUID(pack.context_pack_id),
                    retrieval_profile=pack.retrieval_profile,
                    returned_artifact_ids=[card.artifact_id],
                    reused_evidence_ids=[card.artifact_id],
                    tokens_returned=tokens_returned,
                    latency_ms=int((time.monotonic() - started) * 1000),
                ),
            )
        logger.info(
            "broker.open_evidence context_pack_id=%s subject=%s evidence_id=%s status=%s tokens=%d",
            pack.context_pack_id,
            subject,
            request.evidence_id,
            status,
            tokens_returned,
        )

    async with pack.lock:
        if cost > pack.run_remaining_tokens:
            await write_ledger("denied", 0)
            raise ToolError(
                f"run budget exceeded: expanding {request.evidence_id} costs {cost} tokens "
                f"but only {pack.run_remaining_tokens} remain"
            )

        allowance = deps.budget_policy.allowance_for(subject)
        usage = pack.usage_for(subject)
        if usage.tokens + cost > allowance.max_tokens:
            await write_ledger("denied", 0)
            raise ToolError(
                f"agent token allowance exceeded: expanding {request.evidence_id} costs {cost} "
                f"tokens but {usage.tokens} of {allowance.max_tokens} are already used"
            )

        content = body[: request.max_tokens * CHARS_PER_TOKEN]
        pack.charge(subject, cost)
        await write_ledger("approved", cost)

    return OpenEvidenceResponse(
        evidence_id=request.evidence_id,
        level="L2",
        untrusted_content=content,
        tokens_used=cost,
        budget_remaining_tokens=pack.run_remaining_tokens,
        source_uri=card.source_uri,
    )
