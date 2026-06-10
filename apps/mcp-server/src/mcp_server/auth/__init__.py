"""Authentication boundary for the MCP server (architecture §12)."""

from mcp_server.auth.entra import build_entra_verifier

__all__ = ["build_entra_verifier"]
