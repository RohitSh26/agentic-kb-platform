"""Optional kb_search over MCP streamable HTTP (env-gated; the default is none).

A minimal JSON-RPC client (initialize -> initialized -> tools/call kb_search)
against the platform's MCP server. Results are UNTRUSTED content — the graph
fences them exactly like diff text. Failures are fail-soft: a review must never
be blocked by the KB being down; the panel logs and proceeds without it.
"""

import json
from typing import Any, Protocol, cast

import httpx

from review_panel.domain.errors import KBSearchError
from review_panel.structured_logging import get_logger

logger = get_logger("review_panel.infrastructure.kb_search")

_TIMEOUT_SECONDS = 30.0
_PROTOCOL_VERSION = "2025-06-18"


class KBSearchClient(Protocol):
    async def search(self, query: str) -> str: ...


class NullKBSearch:
    """No KB configured: every search is empty. The panel reviews the diff alone."""

    async def search(self, query: str) -> str:
        return ""


def _parse_rpc_response(response: httpx.Response) -> dict[str, Any]:
    """Streamable HTTP answers either JSON or a single-message SSE stream."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload: dict[str, Any] = json.loads(line[len("data:") :].strip())
                return payload
        raise KBSearchError("SSE response carried no data message")
    body: dict[str, Any] = response.json()
    return body


class McpHttpKBSearch:
    def __init__(self, url: str, token: str | None) -> None:
        self._url = url
        self._token = token

    def _headers(self, session_id: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if session_id:
            headers["mcp-session-id"] = session_id
        return headers

    async def _rpc(
        self,
        client: httpx.AsyncClient,
        session_id: str | None,
        method: str,
        params: dict[str, Any] | None,
        *,
        rpc_id: int | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if rpc_id is not None:
            message["id"] = rpc_id
        response = await client.post(self._url, headers=self._headers(session_id), json=message)
        if response.status_code >= 400:
            raise KBSearchError(f"MCP endpoint returned {response.status_code} for {method}")
        new_session = response.headers.get("mcp-session-id", session_id)
        if rpc_id is None:  # notification: no body expected
            return None, new_session
        return _parse_rpc_response(response), new_session

    async def search(self, query: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                init_params = {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "review-panel", "version": "0.1.0"},
                }
                _, session_id = await self._rpc(client, None, "initialize", init_params, rpc_id=1)
                await self._rpc(client, session_id, "notifications/initialized", None, rpc_id=None)
                result, _ = await self._rpc(
                    client,
                    session_id,
                    "tools/call",
                    {"name": "kb_search", "arguments": {"query": query}},
                    rpc_id=2,
                )
        except (httpx.HTTPError, json.JSONDecodeError, KBSearchError) as exc:
            # fail-soft: log the failure as a KB-gap signal, review without KB context
            logger.warning("event=kb_search_failed error=%s detail=%s", type(exc).__name__, exc)
            return ""
        text = _extract_text(result)
        logger.info("event=kb_search query_chars=%s result_chars=%s", len(query), len(text))
        return text


def _extract_text(rpc_result: dict[str, Any] | None) -> str:
    if not rpc_result:
        return ""
    result_obj = rpc_result.get("result")
    if not isinstance(result_obj, dict):
        return ""
    result = cast("dict[str, Any]", result_obj)
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block_obj in cast("list[Any]", content):
        if not isinstance(block_obj, dict):
            continue
        block = cast("dict[str, Any]", block_obj)
        text = block.get("text")
        if block.get("type") == "text" and isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)
