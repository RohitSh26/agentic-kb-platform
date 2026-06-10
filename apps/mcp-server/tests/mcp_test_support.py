"""Shared test support: fake token verifier + session-factory builder.

The auth seam is fastmcp's TokenVerifier base class; FakeVerifier accepts one
known bearer token and rejects everything else, so the boundary is exercised
without Entra ID or any network access.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

VALID_TOKEN = "valid-agent-token"
AGENT_SUBJECT = "impl-agent"
MCP_PATH = "/mcp/"


class FakeVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if token == VALID_TOKEN:
            return AccessToken(
                token=token,
                client_id="impl-agent-client",
                scopes=[],
                subject=AGENT_SUBJECT,
            )
        return None


def make_session_factory() -> async_sessionmaker[AsyncSession]:
    # engines connect lazily, so a placeholder URL is fine for tests that
    # never hit /health
    url = TEST_DATABASE_URL or "postgresql+asyncpg://unused@localhost:5432/unused"
    engine = create_async_engine(url)
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def asgi_http_client(server: FastMCP) -> AsyncIterator[httpx.AsyncClient]:
    """In-process HTTP client against the real (auth-enforcing) ASGI app.

    A context manager rather than a fixture on purpose: the app lifespan's
    anyio cancel scope must enter and exit in the same task, and pytest-asyncio
    runs fixture setup/teardown in different tasks.
    """
    app = server.http_app(path=MCP_PATH, stateless_http=True)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
