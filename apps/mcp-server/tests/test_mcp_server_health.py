from mcp_server import health


def test_health() -> None:
    assert health() == {"status": "ok", "service": "mcp-server"}
