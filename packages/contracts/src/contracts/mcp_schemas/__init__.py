"""Versioned request/response schemas for MCP Context Broker tools.

Every context.* / graph.* / ledger.* tool gets a request and response model
here before it is implemented in apps/mcp-server. TOOL_SCHEMAS is the
authoritative tool surface the server registers from.
"""

from contracts.mcp_schemas.base import McpModel
from contracts.mcp_schemas.context import (
    CreatePackRequest,
    CreatePackResponse,
    OpenEvidenceRequest,
    OpenEvidenceResponse,
    ReadPackRequest,
    ReadPackResponse,
    RequestMoreRequest,
    RequestMoreResponse,
    RequestMoreStatus,
)
from contracts.mcp_schemas.evidence import AgentRole, EvidenceCard, EvidenceLevel
from contracts.mcp_schemas.graph import GetNeighborsRequest, GetNeighborsResponse, GraphNeighbor
from contracts.mcp_schemas.ledger import (
    ListRetrievalsRequest,
    ListRetrievalsResponse,
    RetrievalEventRecord,
)
from contracts.mcp_schemas.registry import TOOL_SCHEMAS, ToolSchema
from contracts.versions import MCP_SCHEMA_VERSION

__all__ = [
    "MCP_SCHEMA_VERSION",
    "TOOL_SCHEMAS",
    "AgentRole",
    "CreatePackRequest",
    "CreatePackResponse",
    "EvidenceCard",
    "EvidenceLevel",
    "GetNeighborsRequest",
    "GetNeighborsResponse",
    "GraphNeighbor",
    "ListRetrievalsRequest",
    "ListRetrievalsResponse",
    "McpModel",
    "OpenEvidenceRequest",
    "OpenEvidenceResponse",
    "ReadPackRequest",
    "ReadPackResponse",
    "RequestMoreRequest",
    "RequestMoreResponse",
    "RequestMoreStatus",
    "RetrievalEventRecord",
    "ToolSchema",
]
