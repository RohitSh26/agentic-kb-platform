"""governance.checkpoint: durable gate-decision events for the ledger.

The agent runner calls ``record_checkpoint`` at each delegation gate
to write a ``governance.checkpoint`` row into the retrieval_event ledger. This
is NOT an MCP tool — it is an internal broker call made by the runner when it
approves, edits, rejects, or aborts an agent-to-agent plan handoff.

The event is replayable via `python -m agentic_mcp_server.replay <run_id>` so
an operator can verify every gate decision for a run.
"""

import logging

from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_kb_version
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "governance.checkpoint"
_UNRESOLVED = "-"

# Valid gate decisions.  The status column in the ledger takes these values for
# governance.checkpoint rows (distinct from the broker outcome statuses
# approved/reused/denied/needs_human_approval/error that tool rows carry).
CHECKPOINT_DECISIONS = frozenset({"approved", "edited", "rejected", "aborted"})


async def record_checkpoint(
    deps: BrokerDeps,
    *,
    run_id: str,
    from_agent: str,
    to_agent: str,
    plan_summary: str,
    decision: str,
    edits: list[str] | None = None,
) -> None:
    """Write a governance.checkpoint ledger row for an agent delegation gate.

    Args:
        deps: Broker dependency container (session factory, settings).
        run_id: The run being gated.
        from_agent: The delegating agent (orchestrator or sub-agent).
        to_agent: The agent that would receive the plan.
        plan_summary: Human-readable summary of the plan being handed off.
            Never includes raw evidence text — routing metadata only.
        decision: Gate outcome — one of ``approved``, ``edited``, ``rejected``,
            ``aborted``.  Values outside CHECKPOINT_DECISIONS are accepted
            but logged as warnings (future-proofing).
        edits: Optional list of edit descriptions made to the plan before
            approval.  Only meaningful when ``decision == "edited"``.
    """
    if decision not in CHECKPOINT_DECISIONS:
        logger.warning(
            "event=governance_checkpoint_unknown_decision run_id=%s decision=%s",
            run_id,
            decision,
        )

    _details: dict[str, object] = {
        "from_agent": from_agent,
        "to_agent": to_agent,
        "plan_summary": plan_summary,
        "decision": decision,
        "edits": edits or [],
    }

    async with deps.session_factory() as session:
        kb_version = await fetch_active_kb_version(session) or _UNRESOLVED
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=run_id,
                agent_name=from_agent,
                tool_name=_TOOL_NAME,
                status=decision,
                kb_version=kb_version,
                details=_details,
            ),
        )

    logger.info(
        "event=governance_checkpoint run_id=%s from_agent=%s to_agent=%s decision=%s",
        run_id,
        from_agent,
        to_agent,
        decision,
    )
