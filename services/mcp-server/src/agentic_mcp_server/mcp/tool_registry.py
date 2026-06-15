"""Authoritative tool-name → request/response schema registry.

The MCP server registers its tool surface from this table, so a tool cannot
exist at the boundary without a versioned contract (schema before code; see
docs/contracts/mcp-tools-contract.md).
"""

from dataclasses import dataclass

from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.context import (
    CreatePackRequest,
    CreatePackResponse,
    OpenEvidenceRequest,
    OpenEvidenceResponse,
    ReadPackRequest,
    ReadPackResponse,
    RequestMoreRequest,
    RequestMoreResponse,
)
from agentic_mcp_server.mcp.tool_schemas.graph import GetNeighborsRequest, GetNeighborsResponse
from agentic_mcp_server.mcp.tool_schemas.ledger import (
    ListRetrievalsRequest,
    ListRetrievalsResponse,
)
from agentic_mcp_server.mcp.tool_schemas.verification import (
    PlatformTrustDecision,
    PlatformTrustRequest,
    VerificationReceipt,
    VerifyAnswerRequest,
)


@dataclass(frozen=True)
class ToolSchema:
    request: type[McpModel]
    response: type[McpModel]


TOOL_SCHEMAS: dict[str, ToolSchema] = {
    "context.create_pack": ToolSchema(CreatePackRequest, CreatePackResponse),
    "context.read_pack": ToolSchema(ReadPackRequest, ReadPackResponse),
    "context.request_more": ToolSchema(RequestMoreRequest, RequestMoreResponse),
    "context.open_evidence": ToolSchema(OpenEvidenceRequest, OpenEvidenceResponse),
    "graph.get_neighbors": ToolSchema(GetNeighborsRequest, GetNeighborsResponse),
    "ledger.list_retrievals": ToolSchema(ListRetrievalsRequest, ListRetrievalsResponse),
    "context.verify_answer": ToolSchema(VerifyAnswerRequest, VerificationReceipt),
    "context.platform_trust": ToolSchema(PlatformTrustRequest, PlatformTrustDecision),
}
