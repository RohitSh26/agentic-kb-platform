"""Request/response schemas for the graph.* Context Broker tools.

Graph behavior is exposed only through these tools (invariant 2); the
Postgres knowledge_edge backing store stays swappable.
"""

import uuid
from typing import Literal

from pydantic import Field

from contracts.mcp_schemas.base import McpModel


class GetNeighborsRequest(McpModel):
    """Neighbor lookup; returns card metadata only, never raw text.

    Carries no agent/run identity on purpose: ledger attribution and budget
    identity come from the authenticated MCP session.
    """

    artifact_id: uuid.UUID
    edge_types: list[str] = Field(default_factory=list)  # empty = all edge types
    depth: int = Field(default=1, ge=1, le=3)


class GraphNeighbor(McpModel):
    artifact_id: uuid.UUID
    title: str
    artifact_type: str
    edge_type: str
    direction: Literal["out", "in"]
    confidence: float = Field(ge=0.0, le=1.0)
    edge_source: str
    distance: int = Field(ge=1)


class GetNeighborsResponse(McpModel):
    artifact_id: uuid.UUID
    kb_version: str = Field(min_length=1)
    neighbors: list[GraphNeighbor]
