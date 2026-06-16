"""Observability details integration tests.

Verifies that:
  - create_pack writes a details payload that round-trips through the ledger.
  - record_checkpoint writes a governance.checkpoint row with decision + details.
  - existing tool contracts are unbroken (ledger rows still appear as before).

Requires an externally migrated TEST_DATABASE_URL at migration head (0017+).
Skips gracefully when no database is configured.
"""

import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    insert_artifact,
    insert_build_run,
    make_broker_deps,
    require_registry_schema,
)
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.checkpoint import record_checkpoint
from agentic_mcp_server.context_broker.pack import create_pack
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.mcp.tool_schemas.context import CreatePackRequest

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "impl-agent"
REQUESTER = Requester(subject=SUBJECT, teams=frozenset())
RUN_ID = "run-obs-1"


@pytest.fixture()
def factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory()


@pytest.fixture(autouse=True)
async def registry(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
        await insert_build_run(session, KB_VERSION, "active")


async def _fetch_details(
    factory: async_sessionmaker[AsyncSession], run_id: str, tool_name: str
) -> dict | None:  # type: ignore[type-arg]
    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT details FROM retrieval_event"
                " WHERE run_id = :run_id AND tool_name = :tool_name"
                " ORDER BY created_at LIMIT 1"
            ),
            {"run_id": run_id, "tool_name": tool_name},
        )
        return result.scalar_one_or_none()


async def test_create_pack_details_round_trips(factory: async_sessionmaker[AsyncSession]) -> None:
    """create_pack writes a structured details payload that survives the DB round-trip."""
    search = FakeSearchClient()
    async with factory() as session:
        artifact_id = await insert_artifact(
            session,
            title="Auth service overview",
            body_text="The auth service validates JWTs and issues access tokens.",
        )
    search.seed("auth", [SearchHit(artifact_id=artifact_id, score=1.5)])
    deps = make_broker_deps(factory, search)

    await create_pack(
        deps,
        CreatePackRequest(
            run_id=RUN_ID,
            task="auth service",
            approved_context_plan="review auth token validation",
            retrieval_profile="default",
            budget_tokens=8000,
        ),
        REQUESTER,
    )

    details = await _fetch_details(factory, RUN_ID, "context.create_pack")
    assert details is not None, "details must be written for create_pack"
    assert details["task"] == "auth service"
    assert isinstance(details["cards"], list)
    assert isinstance(details["budget"], dict)
    assert details["budget"]["allowed"] == 8000
    assert details["budget"]["used"] >= 0
    # At least one card for the artifact we seeded.
    assert len(details["cards"]) >= 1
    card = details["cards"][0]
    assert "artifact_id" in card
    assert "title" in card
    assert "score" in card
    assert "card_type" in card


async def test_create_pack_details_null_when_no_cards(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """When no evidence is found, create_pack still writes details (empty cards list)."""
    deps = make_broker_deps(factory, FakeSearchClient())

    await create_pack(
        deps,
        CreatePackRequest(
            run_id="run-obs-empty",
            task="nonexistent topic",
            approved_context_plan="nothing here",
            retrieval_profile="default",
            budget_tokens=8000,
        ),
        REQUESTER,
    )

    details = await _fetch_details(factory, "run-obs-empty", "context.create_pack")
    assert details is not None
    assert details["cards"] == []
    assert details["task"] == "nonexistent topic"


async def test_record_checkpoint_writes_governance_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """record_checkpoint inserts a governance.checkpoint row with decision + details."""
    deps = make_broker_deps(factory, FakeSearchClient())

    await record_checkpoint(
        deps,
        run_id=RUN_ID,
        from_agent="orchestrator",
        to_agent="impl-agent",
        plan_summary="Implement the auth module refactor",
        decision="approved",
        edits=None,
    )

    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT tool_name, status, agent_name, details"
                " FROM retrieval_event"
                " WHERE run_id = :run_id AND tool_name = 'governance.checkpoint'"
            ),
            {"run_id": RUN_ID},
        )
        row = result.one_or_none()

    assert row is not None, "governance.checkpoint row must be written"
    assert row.tool_name == "governance.checkpoint"
    assert row.status == "approved"
    assert row.agent_name == "orchestrator"
    details = row.details
    assert details is not None
    assert details["from_agent"] == "orchestrator"
    assert details["to_agent"] == "impl-agent"
    assert details["decision"] == "approved"
    assert details["plan_summary"] == "Implement the auth module refactor"
    assert details["edits"] == []


async def test_record_checkpoint_edited_decision(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """record_checkpoint correctly records an edited gate with edit descriptions."""
    deps = make_broker_deps(factory, FakeSearchClient())

    await record_checkpoint(
        deps,
        run_id="run-obs-edited",
        from_agent="orchestrator",
        to_agent="test-agent",
        plan_summary="Run full test suite",
        decision="edited",
        edits=["reduced scope to unit tests only", "removed integration test step"],
    )

    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT status, details FROM retrieval_event"
                " WHERE run_id = 'run-obs-edited' AND tool_name = 'governance.checkpoint'"
            )
        )
        row = result.one_or_none()

    assert row is not None
    assert row.status == "edited"
    assert row.details["decision"] == "edited"
    edits = row.details["edits"]
    assert len(edits) == 2
    assert "reduced scope to unit tests only" in edits
