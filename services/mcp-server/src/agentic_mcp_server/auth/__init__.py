"""Authentication boundary for the MCP server (architecture §12)."""

from agentic_mcp_server.auth.client_identity import (
    ClientIdentity,
    ClientRegistry,
    parse_client_registry,
)
from agentic_mcp_server.auth.entra import build_entra_verifier
from agentic_mcp_server.auth.local_dev import LocalDevIdentity, LocalDevTokenVerifier
from agentic_mcp_server.auth.local_dev_selection import (
    LocalDevAuthRefused,
    build_local_dev_identity,
    select_verifier,
)

__all__ = [
    "ClientIdentity",
    "ClientRegistry",
    "LocalDevAuthRefused",
    "LocalDevIdentity",
    "LocalDevTokenVerifier",
    "build_entra_verifier",
    "build_local_dev_identity",
    "parse_client_registry",
    "select_verifier",
]
