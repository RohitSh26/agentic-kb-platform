"""Shared contract boundary for the Agentic KB Platform.

Exposes three schema namespaces (mcp_schemas, artifact_schemas,
agent_output_schemas) and the version constants used in cache keys.
"""

from contracts import agent_output_schemas, artifact_schemas, mcp_schemas
from contracts.versions import (
    CHUNKER_VERSION,
    GRAPHIFY_VERSION,
    OUTPUT_SCHEMA_VERSION,
    PROMPT_VERSION,
)

__all__ = [
    "CHUNKER_VERSION",
    "GRAPHIFY_VERSION",
    "OUTPUT_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "agent_output_schemas",
    "artifact_schemas",
    "mcp_schemas",
]
