"""Health endpoint tests against a real (local) Postgres registry.

/health is deliberately unauthenticated — these requests carry no
Authorization header. Readiness is defined by invariant 5: ready iff a
kb_build_run row has status='active'.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastmcp import FastMCP
from mcp_test_support import TEST_DATABASE_URL, asgi_http_client, make_session_factory
from sqlalchemy import delete

from db.models import KbBuildRun

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages" / "db" / "alembic.ini"


@pytest.fixture(scope="module")
def migrated_db() -> Iterator[None]:
    assert TEST_DATABASE_URL is not None
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(autouse=True)
async def clean_build_runs(migrated_db: None) -> AsyncIterator[None]:
    factory = make_session_factory()
    async with factory() as session:
        await session.execute(delete(KbBuildRun))
        await session.commit()
    yield


async def test_health_without_active_version_is_not_ready(server: FastMCP) -> None:
    async with asgi_http_client(server) as health_client:
        response = await health_client.get("/health")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "no_active_kb_version"
    assert payload["active_kb_version"] is None
    assert payload["service"] == "mcp-server"


async def test_health_returns_active_kb_version(server: FastMCP) -> None:
    factory = make_session_factory()
    async with factory() as session:
        session.add(KbBuildRun(kb_version="kb-2026-06-10", status="active"))
        session.add(KbBuildRun(kb_version="kb-2026-06-09", status="superseded"))
        await session.commit()
    async with asgi_http_client(server) as health_client:
        response = await health_client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["active_kb_version"] == "kb-2026-06-10"
