import pytest
from fastmcp import FastMCP
from mcp_test_support import FakeVerifier, make_session_factory

from agentic_mcp_server.mcp.server import build_server


@pytest.fixture()
def server() -> FastMCP:
    return build_server(auth=FakeVerifier(), session_factory=make_session_factory())
