"""Static health stub; real readiness checks arrive with the MCP server base (PR-09)."""


def health() -> dict[str, str]:
    return {"status": "ok", "service": "mcp-server"}
