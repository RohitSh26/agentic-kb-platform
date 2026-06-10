"""Versioned output schemas for the product's runtime agents.

Each manifest in agents/ declares an output schema defined here; the MCP
runtime validates agent outputs against these models.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from contracts.versions import OUTPUT_SCHEMA_VERSION

AGENT_OUTPUT_SCHEMA_VERSION = OUTPUT_SCHEMA_VERSION


class AgentOutputModel(BaseModel):
    """Base for all agent output models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = AGENT_OUTPUT_SCHEMA_VERSION


__all__ = ["AGENT_OUTPUT_SCHEMA_VERSION", "AgentOutputModel"]
