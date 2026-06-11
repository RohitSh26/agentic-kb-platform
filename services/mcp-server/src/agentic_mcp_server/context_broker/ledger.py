"""ledger.list_retrievals: read the retrieval_event ledger for a run.

Reads first, then writes its own ledger row — so the listing an agent sees
never includes the listing call itself, but the call is still audited.
"""

import logging
import time

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_kb_version
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
    list_events,
)
from agentic_mcp_server.mcp.tool_schemas.ledger import (
    ListRetrievalsRequest,
    ListRetrievalsResponse,
    RetrievalEventRecord,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "ledger.list_retrievals"


async def list_retrievals(
    deps: BrokerDeps, request: ListRetrievalsRequest, requester: Requester
) -> ListRetrievalsResponse:
    started = time.monotonic()
    async with deps.session_factory() as session:
        rows = await list_events(session, request.run_id)
        kb_version = await fetch_active_kb_version(session) or "-"
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=request.run_id,
                agent_name=requester.subject,
                tool_name=_TOOL_NAME,
                status="approved",
                kb_version=kb_version,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )
    logger.info(
        "broker.list_retrievals run_id=%s subject=%s events=%d",
        request.run_id,
        requester.subject,
        len(rows),
    )
    return ListRetrievalsResponse(
        run_id=request.run_id,
        events=[
            RetrievalEventRecord(
                event_id=row.retrieval_id,
                run_id=row.run_id,
                kb_version=row.kb_version,
                agent_name=row.agent_name,
                tool=row.tool_name,
                status=row.status,
                cache_hit=row.cache_hit,
                tokens_returned=row.tokens_returned,
                evidence_ids=list(
                    dict.fromkeys(str(e) for e in [*row.reused_evidence_ids, *row.new_evidence_ids])
                ),
                created_at=row.created_at,
            )
            for row in rows
        ],
    )
