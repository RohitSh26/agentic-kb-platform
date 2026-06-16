"""graph.get_neighbors: bounded BFS over knowledge_edge, card metadata only.

Graph behavior is exposed only through this tool (invariant 2). The traversal
is depth- and fan-out-capped, returns titles and edge metadata — never raw
text — and every call writes a ledger row (run_id sentinel "-": graph lookups
are not run-scoped). ACL filtering happens per hop, before the frontier
expands: a restricted node must not reveal its connectivity, and the
traversal must not transit through it to authorized nodes beyond.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from fastmcp.exceptions import ToolError

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.error_ledger import write_error_event
from agentic_mcp_server.context_broker.retrieval import authorization_decision
from agentic_mcp_server.context_broker.trust import admits, is_claim_supporting
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_version
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow, fetch_artifacts
from agentic_mcp_server.infrastructure.postgres.edges import fetch_edges_touching
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.mcp.tool_schemas.graph import (
    GetNeighborsRequest,
    GetNeighborsResponse,
    GraphNeighbor,
)
from agentic_mcp_server.telemetry.audit import audit_context_access

logger = logging.getLogger(__name__)

_TOOL_NAME = "graph.get_neighbors"
NO_RUN_SENTINEL = "-"


@dataclass(frozen=True)
class _Found:
    artifact_id: uuid.UUID
    edge_type: str
    direction: Literal["out", "in"]
    confidence: float
    edge_source: str
    distance: int
    trust_class: str


async def get_neighbors(
    deps: BrokerDeps, request: GetNeighborsRequest, requester: Requester
) -> GetNeighborsResponse:
    started = time.monotonic()
    suppressed: list[uuid.UUID] = []
    async with deps.session_factory() as session:
        active = await fetch_active_version(session)
        if active is None:
            await write_error_event(
                deps,
                tool_name=_TOOL_NAME,
                subject=requester.subject,
                query_text=str(request.artifact_id),
            )
            raise ToolError("no active kb_version; the knowledge base has not been built yet")
        kb_version = active.kb_version
        build_seq = active.build_seq

        edge_types = request.edge_types or None
        cap = deps.settings.max_graph_neighbors
        visited: set[uuid.UUID] = {request.artifact_id}
        found: list[_Found] = []
        rows_by_id: dict[uuid.UUID, ArtifactRow] = {}

        # the root is hop 0: an unauthorized start node must not reveal its
        # connectivity, so denial yields the same empty result as an unknown id
        root_rows = await fetch_artifacts(session, [request.artifact_id], build_seq)
        if deps.authorization.filter_artifacts(requester, root_rows):
            frontier = [request.artifact_id]
        else:
            frontier = []
            suppressed.extend(row.artifact_id for row in root_rows)

        for distance in range(1, request.depth + 1):
            if not frontier or len(found) >= cap:
                break
            edges = await fetch_edges_touching(session, frontier, build_seq, edge_types)
            frontier_set = set(frontier)
            candidates: list[_Found] = []
            for edge in edges:
                # Trust admission first: AMBIGUOUS/REJECTED (and unknown buckets)
                # never route or surface; INFERRED_* only with include_inferred.
                # A non-admitted edge must not even transit the frontier.
                if not admits(
                    edge.trust_class,
                    trust_floor=request.trust_floor,
                    include_inferred=request.include_inferred,
                ):
                    continue
                if edge.from_artifact_id in frontier_set and edge.to_artifact_id not in visited:
                    neighbor, direction = edge.to_artifact_id, "out"
                elif edge.to_artifact_id in frontier_set and edge.from_artifact_id not in visited:
                    neighbor, direction = edge.from_artifact_id, "in"
                else:
                    continue
                visited.add(neighbor)
                candidates.append(
                    _Found(
                        artifact_id=neighbor,
                        edge_type=edge.edge_type,
                        direction=direction,
                        confidence=min(max(edge.confidence or 0.0, 0.0), 1.0),
                        edge_source=edge.source or "unknown",
                        distance=distance,
                        trust_class=edge.trust_class,
                    )
                )
            if not candidates:
                frontier = []
                continue

            # filter each hop BEFORE expanding the frontier: an unauthorized
            # node is neither returned nor traversed through
            artifacts = await fetch_artifacts(
                session, [candidate.artifact_id for candidate in candidates], build_seq
            )
            allowed_by_id = {
                artifact.artifact_id: artifact
                for artifact in deps.authorization.filter_artifacts(requester, artifacts)
            }
            suppressed.extend(
                artifact.artifact_id
                for artifact in artifacts
                if artifact.artifact_id not in allowed_by_id
            )

            next_frontier: list[uuid.UUID] = []
            for candidate in candidates:
                row = allowed_by_id.get(candidate.artifact_id)
                if row is None:
                    continue
                if len(found) >= cap:
                    break
                rows_by_id[candidate.artifact_id] = row
                found.append(candidate)
                next_frontier.append(candidate.artifact_id)
            frontier = next_frontier

    neighbors = [
        GraphNeighbor(
            artifact_id=f.artifact_id,
            title=rows_by_id[f.artifact_id].title or str(f.artifact_id),
            artifact_type=rows_by_id[f.artifact_id].artifact_type,
            edge_type=f.edge_type,
            direction=f.direction,
            confidence=f.confidence,
            edge_source=f.edge_source,
            distance=f.distance,
            trust_class=f.trust_class,
            claim_supporting=is_claim_supporting(f.trust_class),
        )
        for f in found
    ]

    # Count neighbors by edge_type for the observability details.
    neighbors_by_type: dict[str, int] = {}
    for n in neighbors:
        neighbors_by_type[n.edge_type] = neighbors_by_type.get(n.edge_type, 0) + 1
    _graph_details: dict[str, object] = {
        "artifact_id": str(request.artifact_id),
        "depth": request.depth,
        "trust_floor": request.trust_floor,
        "neighbors_by_type": neighbors_by_type,
    }

    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=NO_RUN_SENTINEL,
                agent_name=requester.subject,
                tool_name=_TOOL_NAME,
                status="approved",
                kb_version=kb_version,
                query_text=str(request.artifact_id),
                returned_artifact_ids=[n.artifact_id for n in neighbors],
                latency_ms=int((time.monotonic() - started) * 1000),
                details=_graph_details,
            ),
        )
    audit_context_access(
        tool=_TOOL_NAME,
        requester=requester,
        kb_version=kb_version,
        artifact_ids=[n.artifact_id for n in neighbors],
        suppressed_artifact_ids=suppressed,
    )
    logger.info(
        "broker.get_neighbors artifact_id=%s subject=%s depth=%d trust_floor=%s "
        "include_inferred=%s neighbors=%d suppressed=%d",
        request.artifact_id,
        requester.subject,
        request.depth,
        request.trust_floor,
        request.include_inferred,
        len(neighbors),
        len(suppressed),
    )
    return GetNeighborsResponse(
        artifact_id=request.artifact_id,
        kb_version=kb_version,
        neighbors=neighbors,
        authorization=authorization_decision(deps),
    )
