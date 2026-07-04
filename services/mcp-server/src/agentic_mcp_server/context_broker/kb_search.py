"""kb_search: the budgeted, KB-first retrieval tool (ADR-0025, ADR-0030).

One query in, ranked ACL-filtered hits out — the broker ceremony (pack
creation, evidence handles, justification fields) deliberately absent. The ONE
enforced restriction is the dual budget (ADR-0025 §4): a per-window call-count
cap AND a token cap on cumulative KB content returned, both required, checked
with the proven ``_kb_budget_open`` shape from scripts/kb_agent.py. Relevance
is the existing shared retrieval path (SearchClient hints -> Postgres
hydration -> ACL -> rank/dedupe) — no new search or ranking logic here.
"""

import logging
import time

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.budgets import AgentAllowance, AgentUsage, kb_budget_open
from agentic_mcp_server.context_broker.constants import MSG_NO_ACTIVE_VERSION, NO_RUN_SENTINEL
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.error_ledger import (
    LedgeredToolError,
    write_error_event,
)
from agentic_mcp_server.context_broker.retrieval import retrieve_cards
from agentic_mcp_server.domain.query_text import normalize_query
from agentic_mcp_server.domain.token_budget import estimate_tokens
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_version
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.mcp.tool_schemas.search import (
    KbSearchBudget,
    KbSearchHit,
    KbSearchRequest,
    KbSearchResponse,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "kb_search"

# ADR-0025 §4's exact instruction to the agent when the cap is hit — a response,
# never a tool error, so the agent keeps working with files instead of crashing.
BUDGET_SPENT_NOTICE = (
    "KB budget spent — work with what you have, or read the specific files you still need."
)

# Compact, citable snippet size (kb_agent.py's proven result shape).
_SNIPPET_CHARS = 200


def _hit(artifact: ArtifactRow) -> KbSearchHit:
    # Keyword-ranked hits are `interpreted` (relevance-ranked, not cross-validated);
    # a future graph-derived path constructs hits with tier="deterministic" — the
    # schema's ConfidenceTier already admits it (tool_schemas/search.py).
    return KbSearchHit(
        title=artifact.title or artifact.artifact_type,
        artifact_type=artifact.artifact_type,
        source_uri=artifact.source_uri,
        snippet=" ".join((artifact.body_text or "").split())[:_SNIPPET_CHARS],
        confidence_tier="interpreted",
    )


def _remaining(allowance: AgentAllowance, usage: AgentUsage) -> KbSearchBudget:
    return KbSearchBudget(
        calls=max(allowance.max_requests - usage.requests, 0),
        tokens=max(allowance.max_tokens - usage.tokens, 0),
    )


async def kb_search(
    deps: BrokerDeps, request: KbSearchRequest, requester: Requester, *, session_key: str
) -> KbSearchResponse:
    started = time.monotonic()
    async with deps.session_factory() as session:
        active = await fetch_active_version(session)
    if active is None:
        await write_error_event(
            deps, tool_name=_TOOL_NAME, subject=requester.subject, query_text=request.query
        )
        raise LedgeredToolError(MSG_NO_ACTIVE_VERSION)

    normalized = normalize_query(request.query)
    allowance = deps.budget_policy.allowance_for(requester.subject)
    window = deps.kb_search_usage.window_for(session_key, requester.subject)

    async def write_ledger(status: str, *, returned: list[ArtifactRow], tokens: int) -> None:
        async with deps.session_factory() as session:
            await insert_event(
                session,
                RetrievalEventInsert(
                    # the request carries no run handle by contract; the session is
                    # recorded in details for operator grouping
                    run_id=NO_RUN_SENTINEL,
                    agent_name=requester.subject,
                    tool_name=_TOOL_NAME,
                    status=status,
                    kb_version=active.kb_version,
                    query_text=request.query,
                    normalized_query=normalized,
                    returned_artifact_ids=[artifact.artifact_id for artifact in returned],
                    tokens_returned=tokens,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    details={
                        "session": session_key,
                        "calls_used": window.usage.requests,
                        "tokens_used": window.usage.tokens,
                        "max_requests": allowance.max_requests,
                        "max_tokens": allowance.max_tokens,
                    },
                ),
            )

    async with window.lock:
        if not kb_budget_open(allowance, window.usage):
            await write_ledger("denied", returned=[], tokens=0)
            logger.info(
                "broker.kb_search subject=%s status=denied calls_used=%d/%d tokens_used=%d/%d",
                requester.subject,
                window.usage.requests,
                allowance.max_requests,
                window.usage.tokens,
                allowance.max_tokens,
            )
            return KbSearchResponse(
                results=[],
                budget_remaining=_remaining(allowance, window.usage),
                notice=BUDGET_SPENT_NOTICE,
            )

        # Snapshot BEFORE charging: if anything below raises (retrieval, cache,
        # or the ledger write itself), the whole charge for this call is
        # refunded — a crashed platform call must never eat the agent's window.
        # The refund is inside this SAME lock acquisition as the charge (no
        # separate lock/unlock round trip), so it shares the charge's exact
        # serialization discipline: no concurrent call can observe the charged-
        # but-not-yet-refunded state as a stable value.
        requests_before, tokens_before = window.usage.requests, window.usage.tokens
        window.usage.requests += 1
        try:
            # retrieve_cards is the ONE shared retrieval idiom: SearchClient relevance
            # hints (PostgresKeywordSearchClient in production), Postgres hydration,
            # team-ACL filtering, transparent rank/dedupe, and the access audit log.
            _, artifacts = await retrieve_cards(
                deps,
                query=request.query,
                kb_version=active.kb_version,
                build_seq=active.build_seq,
                requester=requester,
                tool=_TOOL_NAME,
            )
            hits = [_hit(artifact) for artifact in artifacts]
            # charge the EXACT serialized payload (the same meter==wire rule as
            # card_tokens); charged after the answer, so the final in-budget call may
            # overdraw the token cap — kb_budget_open then refuses the next call
            tokens = sum(estimate_tokens(hit.model_dump_json()) for hit in hits)
            window.usage.tokens += tokens
            spent = not kb_budget_open(allowance, window.usage)
            # snapshot inside the lock: the response states the budget as of THIS
            # call's completion, not whatever a concurrent call charged afterwards
            remaining = _remaining(allowance, window.usage)
            calls_used, tokens_used = window.usage.requests, window.usage.tokens
            await write_ledger("approved", returned=artifacts, tokens=tokens)
        except Exception:
            refunded_tokens = window.usage.tokens - tokens_before
            window.usage.requests, window.usage.tokens = requests_before, tokens_before
            logger.warning(
                "broker.kb_search subject=%s status=refunded calls_refunded=1 "
                "tokens_refunded=%d",
                requester.subject,
                refunded_tokens,
            )
            # Not ledgered here: the uniform tool wrapper (mcp/tool_handlers.py)
            # writes the single error retrieval_event for this call.
            raise

    logger.info(
        "broker.kb_search subject=%s status=approved results=%d tokens_returned=%d "
        "calls_used=%d/%d tokens_used=%d/%d budget_spent=%s",
        requester.subject,
        len(hits),
        tokens,
        calls_used,
        allowance.max_requests,
        tokens_used,
        allowance.max_tokens,
        spent,
    )
    return KbSearchResponse(
        results=hits,
        budget_remaining=remaining,
        notice=BUDGET_SPENT_NOTICE if spent else None,
    )
