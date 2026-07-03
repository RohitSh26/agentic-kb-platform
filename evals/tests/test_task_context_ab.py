"""Hermetic gate for the get_task_context A/B golden set (PR-39).

For every case in agent_task_cases/task_context_ab_v1.yaml: seed its fixture KB
into the migrated registry, run the REAL broker tool in-process (no LLM, no
LangSmith, no Azure), and assert the one-call output covers the hand-written
expected file set — the tool must leave the host agent no reason to fall back
to blind exploration. The live two-arm comparison (LLM-driven) is
scripts/eval_task_context.py and is deliberately not part of this suite.
"""

import os
from pathlib import Path

import pytest
from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.task_context import get_task_context
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp.tool_schemas.task_context import (
    GetTaskContextRequest,
    GetTaskContextResponse,
    TaskContextHints,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from harness.fixtures import (
    RegistryNotMigratedError,
    clean_registry,
    require_registry_schema,
)
from harness.task_context_ab import SUITE, TaskContextAbCase, load_ab_cases, seed_ab_case

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
CASES_PATH = Path(__file__).resolve().parent.parent / "agent_task_cases" / "task_context_ab_v1.yaml"

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="TEST_DATABASE_URL not set (needs a migrated registry: make migrate-test-db)",
)

REQUESTER = Requester(subject="task-context-ab", teams=frozenset())


def test_the_golden_set_loads_and_has_ten_hand_written_cases() -> None:
    cases = load_ab_cases(CASES_PATH)
    assert len(cases) == 10, f"{SUITE} pins ten expert-labelled cases"
    assert all(case.expected_files for case in cases)


def _surfaced_paths(response: GetTaskContextResponse) -> set[str]:
    scope = {entity.path for entity in response.resolved_scope.entities}
    blast = {
        entry.path
        for entry in (
            *response.blast_radius.callers,
            *response.blast_radius.callees,
            *response.blast_radius.tests,
        )
    }
    return scope | blast


async def _run_case(case: TaskContextAbCase) -> GetTaskContextResponse:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    search = FakeSearchClient()
    try:
        async with factory() as session:
            try:
                await require_registry_schema(session)
            except RegistryNotMigratedError as error:
                pytest.skip(str(error))
            await clean_registry(session)
            await seed_ab_case(session, case, search)
        hints = (
            TaskContextHints(
                file_paths=case.hints.file_paths, symbols=case.hints.symbols
            )
            if case.hints is not None
            else None
        )
        deps = BrokerDeps(session_factory=factory, search_client=search)
        try:
            return await get_task_context(
                deps,
                GetTaskContextRequest(task_description=case.task, hints=hints),
                REQUESTER,
            )
        finally:
            async with factory() as session:
                await clean_registry(session)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("case", load_ab_cases(CASES_PATH), ids=lambda case: case.id)
async def test_tool_output_covers_the_expected_file_set(case: TaskContextAbCase) -> None:
    response = await _run_case(case)

    surfaced = _surfaced_paths(response)
    missing = set(case.expected_files) - surfaced
    assert not missing, (
        f"{case.id}: get_task_context left the agent without {sorted(missing)} "
        f"(surfaced: {sorted(surfaced)})"
    )
    # the one-call contract holds: cited, budgeted, and honest
    assert response.evidence_ids
    assert response.budget_used.calls >= 1
    assert response.resolved_scope.entities, f"{case.id}: scope must resolve, not guess"
