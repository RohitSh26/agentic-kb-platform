"""Health endpoint tests against a real (local) Postgres registry.

/health is deliberately unauthenticated — these requests carry no
Authorization header. Readiness is defined by invariant 5: ready iff a
kb_build_run row has status='active'.

mcp-server never runs migrations (kb-builder owns the schema), so these tests
require an externally migrated database: run kb-builder's `alembic upgrade
head` against TEST_DATABASE_URL first (`make migrate-test-db`). If the
kb_build_run table is absent, the tests skip with that instruction.
"""

from collections.abc import AsyncIterator

import pytest
from fastmcp import FastMCP
from mcp_test_support import TEST_DATABASE_URL, asgi_http_client, make_session_factory
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)


@pytest.fixture(autouse=True)
async def clean_build_runs() -> AsyncIterator[None]:
    factory = make_session_factory()
    async with factory() as session:
        table = await session.execute(text("SELECT to_regclass('kb_build_run')"))
        if table.scalar_one_or_none() is None:
            pytest.skip(
                "kb_build_run table missing — run kb-builder migrations first "
                "(make migrate-test-db)"
            )
        await session.execute(text("DELETE FROM kb_build_run"))
        await session.commit()
    yield


async def _insert_build_run(kb_version: str, status: str) -> None:
    factory = make_session_factory()
    async with factory() as session:
        await session.execute(
            text("INSERT INTO kb_build_run (kb_version, status) VALUES (:kb_version, :status)"),
            {"kb_version": kb_version, "status": status},
        )
        await session.commit()


async def test_health_without_active_version_is_not_ready(server: FastMCP) -> None:
    async with asgi_http_client(server) as health_client:
        response = await health_client.get("/health")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "no_active_kb_version"
    assert payload["active_kb_version"] is None
    assert payload["service"] == "mcp-server"


async def test_health_returns_active_kb_version(server: FastMCP) -> None:
    await _insert_build_run("kb-2026-06-10", "active")
    await _insert_build_run("kb-2026-06-09", "superseded")
    async with asgi_http_client(server) as health_client:
        response = await health_client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["active_kb_version"] == "kb-2026-06-10"
