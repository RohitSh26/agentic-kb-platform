"""Base model for all MCP tool request/response schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from contracts.versions import MCP_SCHEMA_VERSION


class McpModel(BaseModel):
    """Base for all MCP tool request/response models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = MCP_SCHEMA_VERSION
