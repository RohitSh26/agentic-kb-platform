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

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.error_ledger import (
    LedgeredToolError,
    write_error_event,
)
from agentic_mcp_server.context_broker.retrieval import authorization_decision
from agentic_mcp_server.context_broker.state import UnknownPackError
from agentic_mcp_server.context_broker.untrusted import scan_for_injection
from agentic_mcp_server.domain.token_budget import CHARS_PER_TOKEN, estimate_tokens
from agentic_mcp_server.infrastructure.postgres.artifacts import fetch_artifacts
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.mcp.tool_schemas.context import OpenEvidenceRequest, OpenEvidenceResponse
from agentic_mcp_server.telemetry.audit import audit_context_access

logger = logging.getLogger(__name__)

_TOOL_NAME = "context.open_evidence"


async def open_evidence(
    deps: BrokerDeps, request: OpenEvidenceRequest, requester: Requester
) -> OpenEvidenceResponse:
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
        raise LedgeredToolError(f"unknown context_pack_id: {request.context_pack_id}") from None

    async def write_audit_error() -> None:
        await write_error_event(
            deps,
            tool_name=_TOOL_NAME,
            subject=requester.subject,
            run_id=pack.run_id,
            kb_version=pack.kb_version,
            context_pack_id=uuid.UUID(pack.context_pack_id),
            query_text=request.evidence_id,
        )

    card = pack.cards.get(request.evidence_id)
    if card is None:
        await write_audit_error()
        # same message as the ACL-denied branch: not-in-pack vs restricted
        # must be indistinguishable to the caller
        raise LedgeredToolError(f"evidence not available: {request.evidence_id}")

    async with deps.session_factory() as session:
        artifacts = await fetch_artifacts(session, [card.artifact_id], pack.build_seq)
    allowed = deps.authorization.filter_artifacts(requester, artifacts)
    if not allowed:
        audit_context_access(
            tool=_TOOL_NAME,
            requester=requester,
            kb_version=pack.kb_version,
            artifact_ids=[],
            suppressed_artifact_ids=[card.artifact_id],
        )
        await write_audit_error()
        raise LedgeredToolError(f"evidence not available: {request.evidence_id}")
    body = allowed[0].body_text or ""

    cost = min(estimate_tokens(body), request.max_tokens)

    async def write_ledger(
        status: str, tokens_returned: int, injection_flagged: bool = False
    ) -> None:
        _ev_details: dict[str, object] = {
            "evidence_id": request.evidence_id,
            "level": "L2",
            "injection_flagged": injection_flagged,
            "tokens": tokens_returned,
        }
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
                    retrieval_profile=pack.retrieval_profile,
                    returned_artifact_ids=[card.artifact_id],
                    # an approved expansion is fresh, charged content (the contract's
                    # duplicate_context_tokens counts the first open_evidence as new,
                    # not reuse); only a non-approved row carries no fresh id
                    new_evidence_ids=[card.artifact_id] if status == "approved" else [],
                    reused_evidence_ids=[] if status == "approved" else [card.artifact_id],
                    tokens_returned=tokens_returned,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    details=_ev_details,
                ),
            )
        logger.info(
            "broker.open_evidence context_pack_id=%s subject=%s evidence_id=%s status=%s tokens=%d",
            pack.context_pack_id,
            requester.subject,
            request.evidence_id,
            status,
            tokens_returned,
        )

    async with pack.lock:
        if cost > pack.run_remaining_tokens:
            await write_ledger("denied", 0)
            raise LedgeredToolError(
                f"run budget exceeded: expanding {request.evidence_id} costs {cost} tokens "
                f"but only {pack.run_remaining_tokens} remain"
            )

        allowance = deps.budget_policy.allowance_for(requester.subject)
        usage = pack.usage_for(requester.subject)
        if usage.tokens + cost > allowance.max_tokens:
            await write_ledger("denied", 0)
            raise LedgeredToolError(
                f"agent token allowance exceeded: expanding {request.evidence_id} costs {cost} "
                f"tokens but {usage.tokens} of {allowance.max_tokens} are already used"
            )

        content = body[: request.max_tokens * CHARS_PER_TOKEN]
        # Scan before ledger write so injection_flagged is in the details row.
        scan = scan_for_injection(content)
        pack.charge(requester.subject, cost)
        await write_ledger("approved", cost, injection_flagged=scan.flagged)

    audit_context_access(
        tool=_TOOL_NAME,
        requester=requester,
        kb_version=pack.kb_version,
        artifact_ids=[card.artifact_id],
        injection_flagged_ids=[card.artifact_id] if scan.flagged else [],
    )
    return OpenEvidenceResponse(
        evidence_id=request.evidence_id,
        level="L2",
        untrusted_content=content,
        tokens_used=cost,
        budget_remaining_tokens=pack.run_remaining_tokens,
        source_uri=card.source_uri,
        authorization=authorization_decision(deps),
        injection_flagged=scan.flagged,
        injection_signals=list(scan.signals),
    )
