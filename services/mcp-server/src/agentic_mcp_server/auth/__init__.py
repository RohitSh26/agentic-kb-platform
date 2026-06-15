"""Authentication boundary for the MCP server (architecture §12)."""

from agentic_mcp_server.auth.client_identity import (
    ClientIdentity,
    ClientRegistry,
    parse_client_registry,
)
from agentic_mcp_server.auth.entra import build_entra_verifier

__all__ = [
    "ClientIdentity",
    "ClientRegistry",
    "build_entra_verifier",
    "parse_client_registry",
]
