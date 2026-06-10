"""Tool handlers for the registered surface.

Every tool is a stub until the Context Broker lands (PR-10): requests are
validated against the contract, then rejected with "not implemented".
"""

from collections.abc import Callable, Coroutine
from typing import Any, NoReturn

from fastmcp.exceptions import ToolError

from agentic_mcp_server.mcp.tool_registry import ToolSchema
from agentic_mcp_server.mcp.tool_schemas.base import McpModel

StubFn = Callable[[McpModel], Coroutine[Any, Any, McpModel]]


def make_stub(tool_name: str, schema: ToolSchema) -> StubFn:
    async def stub(request: McpModel) -> NoReturn:
        raise ToolError(f"{tool_name} is not implemented yet; the Context Broker arrives in PR-10")

    stub.__name__ = tool_name.replace(".", "_")
    # fastmcp derives the input/output schemas from these annotations, so the
    # stub enforces the versioned contract even before the broker exists
    stub.__annotations__ = {"request": schema.request, "return": schema.response}
    return stub
