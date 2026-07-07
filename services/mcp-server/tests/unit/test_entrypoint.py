"""The `python -m agentic_mcp_server` boot path constructs the real app.

A developer on a separate machine starts the server with `uv run python -m
agentic_mcp_server`; that imports `__main__` and calls `create_app()`. This is a
construction smoke test only — it asserts the production entrypoint assembles the
same FastMCP app (auth + full tool surface) without a live database (engines
connect lazily), so the run command in docs/dev-guide/getting-started.md cannot rot silently. It
deliberately does NOT bind a socket.
"""

import importlib
import logging
from collections.abc import Iterator

import pytest
from fastmcp import FastMCP

from agentic_mcp_server.config import SERVER_NAME
from agentic_mcp_server.mcp.server import create_app
from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS
from agentic_mcp_server.structured_logging import PACKAGE_LOGGER

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://unused@localhost:5432/unused",
    "MCP_ENTRA_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "MCP_ENTRA_AUDIENCE": "api://agentic-kb-local",
}


@pytest.fixture(autouse=True)
def _restore_package_logger() -> Iterator[None]:
    # create_app() calls configure_logging(), which flips propagate=False on the
    # package logger; that would leak into sibling caplog-based tests in the same
    # session. Restore it on teardown so this construction smoke test stays inert.
    pkg_logger = logging.getLogger(PACKAGE_LOGGER)
    saved_propagate = pkg_logger.propagate
    try:
        yield
    finally:
        pkg_logger.propagate = saved_propagate


@pytest.fixture()
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("_required_env")
def test_create_app_builds_the_full_authenticated_tool_surface() -> None:
    app = create_app()
    assert isinstance(app, FastMCP)
    assert app.name == SERVER_NAME
    assert app.auth is not None, "the entrypoint must build a fail-closed verifier"


@pytest.mark.usefixtures("_required_env")
async def test_create_app_registers_every_contracted_tool() -> None:
    app = create_app()
    tools = await app.list_tools()
    # wire names are the canonical dotted names with '.' -> '_' (mcp/server.py)
    assert {tool.name for tool in tools} == {name.replace(".", "_") for name in TOOL_SCHEMAS}


@pytest.mark.usefixtures("_required_env")
def test_main_module_exposes_a_main_callable() -> None:
    module = importlib.import_module("agentic_mcp_server.__main__")
    assert callable(module.main)


def test_create_app_fails_closed_without_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="missing required environment variables"):
        create_app()
