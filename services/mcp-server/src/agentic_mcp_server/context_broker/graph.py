"""graph.get_neighbors: bounded BFS over knowledge_edge, card metadata only.

Graph behavior is exposed only through this tool (invariant 2). The traversal
is depth- and fan-out-capped, returns titles and edge metadata — never raw
text — and every call writes a ledger row (run_id sentinel "-": graph lookups
are not run-scoped).
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from fastmcp.exceptions import ToolError

from agentic_mcp_server.context_broker.audit import write_error_event
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_kb_version
from agentic_mcp_server.infrastructure.postgres.artifacts import fetch_artifacts
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


async def get_neighbors(
    deps: BrokerDeps, request: GetNeighborsRequest, subject: str
) -> GetNeighborsResponse:
    started = time.monotonic()
    async with deps.session_factory() as session:
        kb_version = await fetch_active_kb_version(session)
        if kb_version is None:
            await write_error_event(
                deps,
                tool_name=_TOOL_NAME,
                subject=subject,
                query_text=str(request.artifact_id),
            )
            raise ToolError("no active kb_version; the knowledge base has not been built yet")

        edge_types = request.edge_types or None
        cap = deps.settings.max_graph_neighbors
        visited: set[uuid.UUID] = {request.artifact_id}
        frontier = [request.artifact_id]
        found: list[_Found] = []

        for distance in range(1, request.depth + 1):
            if not frontier or len(found) >= cap:
                break
            edges = await fetch_edges_touching(session, frontier, kb_version, edge_types)
            frontier_set = set(frontier)
            next_frontier: list[uuid.UUID] = []
            for edge in edges:
                if len(found) >= cap:
                    break
                if edge.from_artifact_id in frontier_set and edge.to_artifact_id not in visited:
                    neighbor, direction = edge.to_artifact_id, "out"
                elif edge.to_artifact_id in frontier_set and edge.from_artifact_id not in visited:
                    neighbor, direction = edge.from_artifact_id, "in"
                else:
                    continue
                visited.add(neighbor)
                next_frontier.append(neighbor)
                found.append(
                    _Found(
                        artifact_id=neighbor,
                        edge_type=edge.edge_type,
                        direction=direction,
                        confidence=min(max(edge.confidence or 0.0, 0.0), 1.0),
                        edge_source=edge.source or "unknown",
                        distance=distance,
                    )
                )
            frontier = next_frontier

        artifacts = await fetch_artifacts(session, [f.artifact_id for f in found], kb_version)

    allowed = deps.authorization.filter_artifacts(subject, artifacts)
    by_id = {artifact.artifact_id: artifact for artifact in allowed}
    neighbors = [
        GraphNeighbor(
            artifact_id=f.artifact_id,
            title=by_id[f.artifact_id].title or str(f.artifact_id),
            artifact_type=by_id[f.artifact_id].artifact_type,
            edge_type=f.edge_type,
            direction=f.direction,
            confidence=f.confidence,
            edge_source=f.edge_source,
            distance=f.distance,
        )
        for f in found
        if f.artifact_id in by_id
    ]

    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=NO_RUN_SENTINEL,
                agent_name=subject,
                tool_name=_TOOL_NAME,
                status="approved",
                kb_version=kb_version,
                query_text=str(request.artifact_id),
                returned_artifact_ids=[n.artifact_id for n in neighbors],
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )
    logger.info(
        "broker.get_neighbors artifact_id=%s subject=%s depth=%d neighbors=%d",
        request.artifact_id,
        subject,
        request.depth,
        len(neighbors),
    )
    return GetNeighborsResponse(
        artifact_id=request.artifact_id,
        kb_version=kb_version,
        neighbors=neighbors,
    )
