"""get_review_draft: fetch a review-panel draft over MCP (PR-41, ADR-0031).

Read-only, compute-never: this tool NEVER triggers draft computation — the
review-panel service remains the sole writer of the `review_panel` schema
(review-panel.md, ADR-0031). Unlike `kb_search`/`get_task_context` this is
not knowledge retrieval, so it carries no budget window: the only gates are
authentication (every requester is an authenticated MCP session, exactly
like every other tool) and the client-scope check (mcp/tool_handlers.py).

No anticipated-failure branch self-ledgers here (unlike kb_search/
get_task_context's "no active kb_version" check) — this tool has no such
precondition; it is not Knowledge-Registry-scoped at all. Any unexpected
read failure (e.g. the `review_panel` schema/table absent because
`DATABASE_URL` points at a different database than
`REVIEW_PANEL_DATABASE_URL` — a documented limitation, review-panel.md) is
left to propagate and is ledgered exactly once by the uniform tool wrapper
(`mcp/tool_handlers.py`'s `_ledgered`), same as kb_search's own crash path.
"""

import logging
import time

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.constants import NO_RUN_SENTINEL
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.infrastructure.postgres.review_draft import fetch_review_draft
from agentic_mcp_server.mcp.tool_schemas.review_draft import (
    GetReviewDraftRequest,
    GetReviewDraftResponse,
    ReviewDraftRecord,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "get_review_draft"


async def get_review_draft(
    deps: BrokerDeps, request: GetReviewDraftRequest, requester: Requester
) -> GetReviewDraftResponse:
    started = time.monotonic()
    async with deps.session_factory() as session:
        row = await fetch_review_draft(
            session,
            repo=request.repo,
            pr_number=request.pr_number,
            head_sha=request.head_sha,
        )
    found = row is not None

    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                # not a run/build-scoped call: the review_panel schema carries no
                # kb_version, and this lookup happens outside any pack run
                run_id=NO_RUN_SENTINEL,
                agent_name=requester.subject,
                tool_name=_TOOL_NAME,
                status="approved",
                kb_version=NO_RUN_SENTINEL,
                query_text=f"{request.repo}#{request.pr_number}",
                latency_ms=int((time.monotonic() - started) * 1000),
                details={
                    "repo": request.repo,
                    "pr_number": request.pr_number,
                    "head_sha": request.head_sha,
                    "found": found,
                },
            ),
        )

    logger.info(
        "broker.get_review_draft subject=%s repo=%s pr_number=%d found=%s",
        requester.subject,
        request.repo,
        request.pr_number,
        found,
    )
    if row is None:
        return GetReviewDraftResponse(found=False, draft=None)
    return GetReviewDraftResponse(
        found=True,
        draft=ReviewDraftRecord(
            draft_key=row.draft_key,
            repo=row.repo,
            pr_number=row.pr_number,
            head_sha=row.head_sha,
            created_at=row.created_at,
            draft=row.draft,
        ),
    )
