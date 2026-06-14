"""Versioned request/response schemas for MCP Context Broker tools.

Every context.* / graph.* / ledger.* tool gets a request and response model
here before it is implemented. The human-readable contract of record is
docs/contracts/mcp-tools-contract.md; keep both in sync in the same PR.
"""

from agentic_mcp_server.mcp.tool_schemas.base import MCP_SCHEMA_VERSION, McpModel
from agentic_mcp_server.mcp.tool_schemas.context import (
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
from agentic_mcp_server.mcp.tool_schemas.evidence import AgentRole, EvidenceCard, EvidenceLevel
from agentic_mcp_server.mcp.tool_schemas.graph import (
    GetNeighborsRequest,
    GetNeighborsResponse,
    GraphNeighbor,
)
from agentic_mcp_server.mcp.tool_schemas.ledger import (
    ListRetrievalsRequest,
    ListRetrievalsResponse,
    RetrievalEventRecord,
)
from agentic_mcp_server.mcp.tool_schemas.verification import (
    RECEIPT_SCHEMA_VERSION,
    ClaimInput,
    ClaimReceipt,
    L0Checks,
    VerificationReceipt,
    VerifyAnswerRequest,
)

__all__ = [
    "MCP_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "AgentRole",
    "ClaimInput",
    "ClaimReceipt",
    "CreatePackRequest",
    "CreatePackResponse",
    "EvidenceCard",
    "EvidenceLevel",
    "GetNeighborsRequest",
    "GetNeighborsResponse",
    "GraphNeighbor",
    "L0Checks",
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
    "VerificationReceipt",
    "VerifyAnswerRequest",
]
