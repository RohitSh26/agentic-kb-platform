"""Structured request telemetry: one log line per tool call, no silent failures."""

import time
from typing import Any

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import CallToolRequestParams

from common.logging import get_logger

logger = get_logger("mcp_server.telemetry")


def _agent_identity() -> str:
    token = get_access_token()
    if token is None:
        return "unauthenticated"
    return token.subject or token.client_id or "unknown"


def _run_id(arguments: dict[str, Any] | None) -> str | None:
    # tools take a single contract model argument named "request"; run-scoped
    # requests carry run_id inside it
    if not arguments:
        return None
    request = arguments.get("request")
    if isinstance(request, dict):
        run_id = request.get("run_id")  # pyright: ignore[reportUnknownMemberType]
        return run_id if isinstance(run_id, str) else None
    return None


class TelemetryMiddleware(Middleware):
    """Emits event=mcp_request with run/agent/tool/latency for every tool call."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, Any],
    ) -> Any:
        tool = context.message.name
        agent = _agent_identity()
        run_id = _run_id(context.message.arguments)
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            logger.error(
                "event=mcp_request tool=%s agent=%s run_id=%s latency_ms=%.1f "
                "status=error error=%s",
                tool,
                agent,
                run_id,
                latency_ms,
                type(exc).__name__,
            )
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "event=mcp_request tool=%s agent=%s run_id=%s latency_ms=%.1f status=ok",
            tool,
            agent,
            run_id,
            latency_ms,
        )
        return result
