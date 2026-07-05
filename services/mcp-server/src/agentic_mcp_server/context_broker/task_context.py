"""get_task_context: one-call task context on a LangGraph StateGraph (PR-39, ADR-0030 §2).

Zero LLM at query time — all model work happened in the nightly build; this tool
is pure retrieval + assembly. The LangGraph structure IS the point: four
genuinely parallel pure-retrieval nodes (resolve_scope, blast_radius,
conventions, similar_prior_changes) join into a synthesize node, with ONE
conditional broadened retry when scope resolves empty, then an honest answer
with what is known. LangSmith tracing is env-gated (LANGSMITH_TRACING) via
langchain-core's standard mechanism; nothing here requires it to be set.
"""

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.constants import MSG_NO_ACTIVE_VERSION, NO_RUN_SENTINEL
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.error_ledger import (
    LedgeredToolError,
    write_error_event,
)
from agentic_mcp_server.context_broker.task_context_nodes import (
    BlastResolution,
    NodeSpan,
    ScopeResolution,
    TaskContextCtx,
    TaskContextState,
    TaskContextUpdate,
    admits_floor,
    blast_radius_node,
    conventions_node,
    resolve_scope_node,
    similar_prior_changes_node,
)
from agentic_mcp_server.domain.token_budget import estimate_tokens
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_version
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.infrastructure.tracing.trace_sink import Span, emit_span
from agentic_mcp_server.mcp.tool_schemas.task_context import (
    BlastRadius,
    BlastRadiusEntity,
    Convention,
    GetTaskContextRequest,
    GetTaskContextResponse,
    PriorChange,
    ResolvedScope,
    TaskContextBudget,
)
from agentic_mcp_server.telemetry.audit import audit_context_access

logger = logging.getLogger(__name__)

_TOOL_NAME = "get_task_context"
_PARALLEL_NODES = ("resolve_scope", "blast_radius", "conventions", "similar_prior_changes")


def tracing_enabled() -> bool:
    """LangSmith's documented env gate. Used ONLY for the structured log field —
    the actual tracing hookup is langchain-core's own env-driven behavior, so
    the suite runs identically with no LANGSMITH_* env set."""
    return os.environ.get("LANGSMITH_TRACING", "").strip().lower() in {"true", "1"}


# ------------------------------------------------------------------------- synthesize


def _floor_scope(scope: ScopeResolution, request: GetTaskContextRequest) -> ScopeResolution:
    floor = request.confidence_floor
    entities = tuple(e for e in scope.entities if admits_floor(e.confidence_tier, floor))
    return ScopeResolution(
        entities=entities, ambiguous=scope.ambiguous, open_questions=scope.open_questions
    )


def _floor_blast(blast: BlastResolution, request: GetTaskContextRequest) -> BlastResolution:
    floor = request.confidence_floor

    def keep(entries: tuple[BlastRadiusEntity, ...]) -> tuple[BlastRadiusEntity, ...]:
        return tuple(e for e in entries if admits_floor(e.confidence_tier, floor))

    return BlastResolution(
        callers=keep(blast.callers), callees=keep(blast.callees), tests=keep(blast.tests)
    )


def _collect_evidence(
    scope: ScopeResolution,
    blast: BlastResolution,
    conventions: tuple[Convention, ...],
    prior: tuple[PriorChange, ...],
) -> list[uuid.UUID]:
    seen: set[uuid.UUID] = set()
    for entity in scope.entities:
        seen.add(entity.entity_id)
    for candidate in scope.ambiguous:
        seen.update(candidate.candidates)
    for entry in (*blast.callers, *blast.callees, *blast.tests):
        seen.add(entry.entity_id)
    for convention in conventions:
        seen.update(convention.evidence_ids)
    for change in prior:
        seen.update(change.evidence_ids)
    return sorted(seen, key=str)


def _build_response(
    *,
    scope: ScopeResolution,
    blast: BlastResolution,
    conventions: tuple[Convention, ...],
    prior: tuple[PriorChange, ...],
    open_questions: list[str],
    calls_used: int,
    tokens: int = 0,
) -> GetTaskContextResponse:
    return GetTaskContextResponse(
        resolved_scope=ResolvedScope(
            entities=list(scope.entities), ambiguous_candidates=list(scope.ambiguous)
        ),
        blast_radius=BlastRadius(
            callers=list(blast.callers), callees=list(blast.callees), tests=list(blast.tests)
        ),
        conventions=list(conventions),
        similar_prior_changes=list(prior),
        evidence_ids=_collect_evidence(scope, blast, conventions, prior),
        budget_used=TaskContextBudget(tokens=tokens, calls=calls_used),
        open_questions=open_questions,
    )


async def synthesize_node(state: TaskContextState) -> TaskContextUpdate:
    started = time.monotonic()
    ctx = state["ctx"]
    request = ctx.request
    scope = _floor_scope(state.get("scope", ScopeResolution()), request)
    blast = _floor_blast(state.get("blast", BlastResolution()), request)
    floor = request.confidence_floor
    conventions: tuple[Convention, ...] = tuple(
        c for c in state.get("conventions", ()) if admits_floor(c.confidence_tier, floor)
    )
    # Prior changes are keyword-ranked (interpreted-class) content: a floor above
    # `interpreted` forces them out rather than silently blending them in.
    prior: tuple[PriorChange, ...] = (
        state.get("prior_changes", ()) if admits_floor("interpreted", floor) else ()
    )

    raw_scope = state.get("scope", ScopeResolution())
    dropped_by_floor = (
        (len(raw_scope.entities) - len(scope.entities))
        + sum(
            len(raw) - len(kept)
            for raw, kept in (
                (state.get("blast", BlastResolution()).callers, blast.callers),
                (state.get("blast", BlastResolution()).callees, blast.callees),
                (state.get("blast", BlastResolution()).tests, blast.tests),
            )
        )
        + (len(state.get("conventions", ())) - len(conventions))
        + (len(state.get("prior_changes", ())) - len(prior))
    )
    if dropped_by_floor:
        logger.info(
            "event=task_context_floor_filtered subject=%s floor=%s dropped=%d",
            ctx.requester.subject,
            floor,
            dropped_by_floor,
        )

    open_questions = list(scope.open_questions)
    if scope.unresolved and state.get("broadened", False):
        open_questions.append(
            "No entity in the knowledge base could be resolved for this task, even after a "
            "broadened search — the KB may not cover this area yet. Proceed from the task "
            "description and repository files directly."
        )

    # Budget: clamp the requested cap to the server's Evidence-Pack cap (the
    # request value is never an escape hatch), then trim the lowest-value tail
    # until the serialized response fits — the create_pack trim idiom, reused.
    server_cap = ctx.deps.settings.task_context_max_tokens
    cap = min(request.max_tokens or server_cap, server_cap)
    calls_used = state.get("calls_used", 0)

    # Trim state IS the kept lists (popped from the tail, order preserved) —
    # never a value-membership filter over the originals, which would misbehave
    # on equal-valued duplicates (popping one tail copy silently drops or keeps
    # ALL equal items, trimming the wrong entries).
    prior_kept: list[PriorChange] = list(prior)
    conventions_kept: list[Convention] = list(conventions)
    blast_lists: dict[str, list[BlastRadiusEntity]] = {
        "callees": list(blast.callees),
        "callers": list(blast.callers),
        "tests": list(blast.tests),
    }
    trimmed = 0
    while True:
        response = _build_response(
            scope=scope,
            blast=BlastResolution(
                callers=tuple(blast_lists["callers"]),
                callees=tuple(blast_lists["callees"]),
                tests=tuple(blast_lists["tests"]),
            ),
            conventions=tuple(conventions_kept),
            prior=tuple(prior_kept),
            open_questions=open_questions,
            calls_used=calls_used,
        )
        tokens = estimate_tokens(response.model_dump_json())
        if tokens <= cap:
            break
        # drop one item from the lowest-value tail: prior -> conventions ->
        # callees -> callers -> tests; never scope, ambiguity, or open questions
        for bucket in (
            prior_kept,
            conventions_kept,
            blast_lists["callees"],
            blast_lists["callers"],
            blast_lists["tests"],
        ):
            if bucket:
                bucket.pop()
                trimmed += 1
                break
        else:
            break  # nothing trimmable left — the untrimmable core stands
    if trimmed:
        logger.info(
            "event=task_context_budget_trim subject=%s cap=%d trimmed=%d tokens=%d",
            ctx.requester.subject,
            cap,
            trimmed,
            tokens,
        )

    # Stamp the measured cost. The stamp itself only changes the payload by the
    # counter digits; the meter stays deterministic (estimate over the exact
    # serialized content, the kb_search meter==wire rule).
    final = response.model_copy(
        update={"budget_used": TaskContextBudget(tokens=tokens, calls=calls_used)}
    )
    return {"response": final, "node_spans": [NodeSpan("synthesize", started, time.monotonic())]}


# ------------------------------------------------------------------------ graph shape


def _route_after_synthesize(state: TaskContextState) -> str:
    """ONE broadened retry, only when scope resolved truly empty (ambiguity is
    an answer — candidates + open questions — and must not trigger a retry)."""
    scope = state.get("scope", ScopeResolution())
    if scope.unresolved and not state.get("broadened", False):
        return "broaden"
    return END


async def broaden_node(state: TaskContextState) -> TaskContextUpdate:
    logger.info("event=task_context_broadened_retry subject=%s", state["ctx"].requester.subject)
    return {
        "broadened": True,
        "node_spans": [NodeSpan("broaden", time.monotonic(), time.monotonic())],
    }


def _build_graph() -> CompiledStateGraph[
    TaskContextState, None, TaskContextState, TaskContextState
]:
    graph: StateGraph[TaskContextState, None, TaskContextState, TaskContextState] = StateGraph(
        TaskContextState
    )
    graph.add_node("resolve_scope", resolve_scope_node)
    graph.add_node("blast_radius", blast_radius_node)
    graph.add_node("conventions", conventions_node)
    graph.add_node("similar_prior_changes", similar_prior_changes_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_node("broaden", broaden_node)
    for node in _PARALLEL_NODES:
        graph.add_edge(START, node)
    # join: synthesize runs once per pass, after ALL four parallel nodes finish
    graph.add_edge(list(_PARALLEL_NODES), "synthesize")
    graph.add_conditional_edges(
        "synthesize", _route_after_synthesize, {"broaden": "broaden", END: END}
    )
    # the retry re-runs the whole fan-out broadened, then synthesizes again
    for node in _PARALLEL_NODES:
        graph.add_edge("broaden", node)
    return graph.compile()


_GRAPH = _build_graph()


@dataclass(frozen=True)
class TaskContextRunResult:
    """The graph run's outcome plus its structure telemetry (spans, retry, cost)."""

    response: GetTaskContextResponse
    node_spans: tuple[NodeSpan, ...]
    calls_used: int
    suppressed: tuple[uuid.UUID, ...]
    retried: bool


async def run_task_context_graph(ctx: TaskContextCtx) -> TaskContextRunResult:
    """Run the compiled StateGraph for one request. Exposed separately from the
    tool entry so structure tests (fan-out concurrency, single retry) can assert
    on the run telemetry without going through the ledger."""
    # ainvoke's return is typed dict[str, Any] upstream; the graph's state schema
    # IS TaskContextState, so this cast restores what langgraph erased.
    state = cast(
        TaskContextState,
        await _GRAPH.ainvoke(
            {"ctx": ctx, "calls_used": 0, "suppressed": [], "node_spans": []},
            config={"run_name": _TOOL_NAME},
        ),
    )
    response = state.get("response")
    if response is None:  # unreachable: every graph path ends in synthesize
        raise RuntimeError("task-context graph finished without a synthesized response")
    return TaskContextRunResult(
        response=response,
        node_spans=tuple(state.get("node_spans", [])),
        calls_used=state.get("calls_used", 0),
        suppressed=tuple(state.get("suppressed", [])),
        retried=state.get("broadened", False),
    )


# --------------------------------------------------------------------------- tracing


def _node_span_attributes(node: str, response: GetTaskContextResponse) -> dict[str, Any]:
    """Safe, aggregate-only metadata per node (ADR-0032 "No-content rule") — counts and
    ids-cardinality drawn from the already-synthesized (floor-filtered, budget-trimmed)
    response, never raw scope/blast/convention/commit text."""
    if node == "resolve_scope":
        return {
            "entities": len(response.resolved_scope.entities),
            "ambiguous": len(response.resolved_scope.ambiguous_candidates),
        }
    if node == "blast_radius":
        return {
            "callers": len(response.blast_radius.callers),
            "callees": len(response.blast_radius.callees),
            "tests": len(response.blast_radius.tests),
        }
    if node == "conventions":
        return {"conventions": len(response.conventions)}
    if node == "similar_prior_changes":
        return {"prior_changes": len(response.similar_prior_changes)}
    if node == "synthesize":
        return {
            "tokens": response.budget_used.tokens,
            "calls_used": response.budget_used.calls,
            "evidence_ids": len(response.evidence_ids),
            "open_questions": len(response.open_questions),
        }
    return {}  # "broaden" carries no extra metadata beyond its timing


def _to_wall(*, mono_reference: float, wall_reference: datetime, mono_value: float) -> datetime:
    """Convert a `time.monotonic()` reading to wall-clock, via one fixed (mono, wall)
    reference pair taken at the same instant — monotonic values have no fixed epoch
    across processes, so they are never stored directly (docs/contracts/tracing.md)."""
    return wall_reference + timedelta(seconds=mono_value - mono_reference)


def _task_context_spans(
    *,
    trace_id: str,
    root_span_id: uuid.UUID,
    mono_reference: float,
    wall_reference: datetime,
    node_spans: tuple[NodeSpan, ...],
    response: GetTaskContextResponse,
    root_ended_at: datetime,
    root_attributes: dict[str, Any],
) -> list[Span]:
    """One root span for the call plus one span per graph node that actually ran."""
    spans = [
        Span(
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            name=_TOOL_NAME,
            service="mcp-server",
            started_at=wall_reference,
            ended_at=root_ended_at,
            status="ok",
            attributes=root_attributes,
        )
    ]
    for node_span in node_spans:
        spans.append(
            Span(
                trace_id=trace_id,
                span_id=uuid.uuid4(),
                parent_span_id=root_span_id,
                name=node_span.node,
                service="mcp-server",
                started_at=_to_wall(
                    mono_reference=mono_reference,
                    wall_reference=wall_reference,
                    mono_value=node_span.started,
                ),
                ended_at=_to_wall(
                    mono_reference=mono_reference,
                    wall_reference=wall_reference,
                    mono_value=node_span.ended,
                ),
                status="ok",
                attributes=_node_span_attributes(node_span.node, response),
            )
        )
    return spans


# -------------------------------------------------------------------------- tool entry


async def get_task_context(
    deps: BrokerDeps, request: GetTaskContextRequest, requester: Requester
) -> GetTaskContextResponse:
    started = time.monotonic()
    wall_started = datetime.now(UTC)
    async with deps.session_factory() as session:
        active = await fetch_active_version(session)
    if active is None:
        await write_error_event(
            deps,
            tool_name=_TOOL_NAME,
            subject=requester.subject,
            query_text=request.task_description,
        )
        raise LedgeredToolError(MSG_NO_ACTIVE_VERSION)

    ctx = TaskContextCtx(
        deps=deps,
        requester=requester,
        request=request,
        build_seq=active.build_seq,
        kb_version=active.kb_version,
    )
    run = await run_task_context_graph(ctx)
    response = run.response

    node_latency_ms = {
        span.node: int((span.ended - span.started) * 1000) for span in run.node_spans
    }
    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=NO_RUN_SENTINEL,  # not run-scoped, like graph.get_neighbors
                agent_name=requester.subject,
                tool_name=_TOOL_NAME,
                status="approved",
                kb_version=active.kb_version,
                query_text=request.task_description,
                returned_artifact_ids=list(response.evidence_ids),
                tokens_returned=response.budget_used.tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
                details={
                    "entities": len(response.resolved_scope.entities),
                    "ambiguous_candidates": len(response.resolved_scope.ambiguous_candidates),
                    "callers": len(response.blast_radius.callers),
                    "callees": len(response.blast_radius.callees),
                    "tests": len(response.blast_radius.tests),
                    "conventions": len(response.conventions),
                    "similar_prior_changes": len(response.similar_prior_changes),
                    "open_questions": len(response.open_questions),
                    "calls_used": run.calls_used,
                    "retried": run.retried,
                    "confidence_floor": request.confidence_floor,
                    "node_latency_ms": node_latency_ms,
                    "tracing": tracing_enabled(),
                },
            ),
        )
    for span in _task_context_spans(
        trace_id=str(uuid.uuid4()),
        root_span_id=uuid.uuid4(),
        mono_reference=started,
        wall_reference=wall_started,
        node_spans=run.node_spans,
        response=response,
        root_ended_at=datetime.now(UTC),
        root_attributes={"retried": run.retried, "calls_used": run.calls_used},
    ):
        await emit_span(deps.trace_sink, span)
    audit_context_access(
        tool=_TOOL_NAME,
        requester=requester,
        kb_version=active.kb_version,
        artifact_ids=list(response.evidence_ids),
        suppressed_artifact_ids=list(run.suppressed),
    )
    logger.info(
        "broker.get_task_context subject=%s entities=%d ambiguous=%d callers=%d callees=%d "
        "tests=%d conventions=%d prior_changes=%d evidence=%d open_questions=%d tokens=%d "
        "calls=%d retried=%s tracing=%s latency_ms=%d",
        requester.subject,
        len(response.resolved_scope.entities),
        len(response.resolved_scope.ambiguous_candidates),
        len(response.blast_radius.callers),
        len(response.blast_radius.callees),
        len(response.blast_radius.tests),
        len(response.conventions),
        len(response.similar_prior_changes),
        len(response.evidence_ids),
        len(response.open_questions),
        response.budget_used.tokens,
        run.calls_used,
        run.retried,
        tracing_enabled(),
        int((time.monotonic() - started) * 1000),
    )
    return response
