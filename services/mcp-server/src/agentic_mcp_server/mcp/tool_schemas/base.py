"""Base model for all MCP tool request/response schemas.

Bump MCP_SCHEMA_VERSION on any breaking change and update
docs/contracts/mcp-tools-contract.md in the same PR.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

MCP_SCHEMA_VERSION = "1.5.0"


class McpModel(BaseModel):
    """Base for all MCP tool request/response models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.5.0"] = MCP_SCHEMA_VERSION
