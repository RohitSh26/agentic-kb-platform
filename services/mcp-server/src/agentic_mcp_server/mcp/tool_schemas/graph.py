"""Request/response schemas for the graph.* Context Broker tools.

Graph behavior is exposed only through these tools (invariant 2); the
Postgres knowledge_edge backing store stays swappable.
"""

import uuid
from typing import Literal

from pydantic import Field

from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.evidence import AuthorizationDecision

TrustFloor = Literal["EXTRACTED", "INFERRED_HIGH", "INFERRED_LOW"]


class GetNeighborsRequest(McpModel):
    """Neighbor lookup; returns card metadata only, never raw text.

    Carries no agent/run identity on purpose: ledger attribution and budget
    identity come from the authenticated MCP session.

    Trust (docs/contracts/trust-buckets.md): ``trust_floor`` defaults to
    ``EXTRACTED`` so only directly-extracted edges are returned. ``AMBIGUOUS``
    and ``REJECTED`` are never returned. ``INFERRED_*`` edges surface only when
    ``include_inferred=true``, labelled as routing hints that cannot support a
    cited claim.
    """

    artifact_id: uuid.UUID
    edge_types: list[str] = Field(default_factory=list)  # empty = all edge types
    depth: int = Field(default=1, ge=1, le=3)
    trust_floor: TrustFloor = "EXTRACTED"
    include_inferred: bool = False


class GraphNeighbor(McpModel):
    artifact_id: uuid.UUID
    title: str
    artifact_type: str
    edge_type: str
    direction: Literal["out", "in"]
    confidence: float = Field(ge=0.0, le=1.0)
    edge_source: str
    distance: int = Field(ge=1)
    # Trust bucket of the connecting edge (docs/contracts/trust-buckets.md).
    trust_class: str
    # False for any INFERRED_* edge: a routing hint that the verifier must
    # reject as direct claim support. Only EXTRACTED edges are claim-supporting.
    claim_supporting: bool


class GetNeighborsResponse(McpModel):
    artifact_id: uuid.UUID
    kb_version: str = Field(min_length=1)
    neighbors: list[GraphNeighbor]
    authorization: AuthorizationDecision
