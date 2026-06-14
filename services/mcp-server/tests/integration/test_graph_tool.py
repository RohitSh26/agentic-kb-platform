"""graph.get_neighbors against a real (local) Postgres knowledge_edge table.

Graph behavior is exposed only through the MCP tool (invariant 2): bounded
BFS, both directions, edge-type filter, fan-out cap, ledger row per call.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    fetch_ledger_rows,
    insert_artifact,
    insert_build_run,
    insert_edge,
    make_broker_deps,
    require_registry_schema,
)
from fastmcp.exceptions import ToolError
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerSettings
from agentic_mcp_server.context_broker.graph import NO_RUN_SENTINEL, get_neighbors
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp.tool_schemas.graph import GetNeighborsRequest

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "impl-agent"
REQUESTER = Requester(subject=SUBJECT, teams=frozenset())


@pytest.fixture()
def factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory()


@pytest.fixture(autouse=True)
async def registry(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
        await insert_build_run(session, KB_VERSION, "active")
    yield


async def _seed_graph(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A -calls-> B -calls-> D, C -references-> A."""
    a = await insert_artifact(session, title="module A", body_text="a", artifact_type="code_chunk")
    b = await insert_artifact(session, title="module B", body_text="b", artifact_type="code_chunk")
    c = await insert_artifact(session, title="doc C", body_text="c")
    d = await insert_artifact(session, title="module D", body_text="d", artifact_type="code_chunk")
    await insert_edge(session, from_artifact_id=a, to_artifact_id=b, edge_type="calls")
    await insert_edge(session, from_artifact_id=b, to_artifact_id=d, edge_type="calls")
    await insert_edge(session, from_artifact_id=c, to_artifact_id=a, edge_type="references")
    return a, b, c, d


async def test_depth_one_returns_both_directions_with_metadata(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        a, b, c, _ = await _seed_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_neighbors(deps, GetNeighborsRequest(artifact_id=a, depth=1), REQUESTER)

    assert response.kb_version == KB_VERSION
    by_id = {n.artifact_id: n for n in response.neighbors}
    assert set(by_id) == {b, c}
    assert (by_id[b].direction, by_id[b].edge_type, by_id[b].distance) == ("out", "calls", 1)
    assert (by_id[c].direction, by_id[c].edge_type, by_id[c].distance) == ("in", "references", 1)
    assert by_id[b].title == "module B"
    assert by_id[b].artifact_type == "code_chunk"


async def test_depth_two_reaches_transitive_neighbors_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        a, b, c, d = await _seed_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_neighbors(deps, GetNeighborsRequest(artifact_id=a, depth=2), REQUESTER)

    distances = {n.artifact_id: n.distance for n in response.neighbors}
    assert distances == {b: 1, c: 1, d: 2}
    # the start node is never reported as its own neighbor
    assert a not in distances


async def test_edge_type_filter_limits_the_traversal(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        a, b, _, d = await _seed_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_neighbors(
        deps, GetNeighborsRequest(artifact_id=a, edge_types=["calls"], depth=2), REQUESTER
    )

    assert {n.artifact_id for n in response.neighbors} == {b, d}


async def test_fan_out_is_capped_by_settings(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        a, *_ = await _seed_graph(session)
    deps = make_broker_deps(
        factory, FakeSearchClient(), settings=BrokerSettings(max_graph_neighbors=1)
    )

    response = await get_neighbors(deps, GetNeighborsRequest(artifact_id=a, depth=3), REQUESTER)

    assert len(response.neighbors) == 1


async def test_every_lookup_writes_a_ledger_row_with_the_run_sentinel(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        a, *_ = await _seed_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    await get_neighbors(deps, GetNeighborsRequest(artifact_id=a, depth=1), REQUESTER)

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status, row.agent_name) for row in rows] == [
        ("graph.get_neighbors", "approved", SUBJECT)
    ]


async def test_no_active_kb_version_errors(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(text("DELETE FROM kb_build_run"))
        await session.commit()
    deps = make_broker_deps(factory, FakeSearchClient())

    with pytest.raises(ToolError, match="no active kb_version"):
        await get_neighbors(deps, GetNeighborsRequest(artifact_id=uuid.uuid4(), depth=1), REQUESTER)


# ---------------------------------------------------------------------------
# Trust-aware traversal (PR-23 / ADR-0011, docs/contracts/trust-buckets.md)
# ---------------------------------------------------------------------------


async def _seed_trust_graph(
    session: AsyncSession,
) -> dict[str, uuid.UUID]:
    """Root A connects to one neighbor per bucket via depth-1 edges."""
    a = await insert_artifact(session, title="root A", body_text="a", artifact_type="code_chunk")
    extracted = await insert_artifact(session, title="EXTRACTED node", body_text="e")
    inferred_high = await insert_artifact(session, title="INFERRED_HIGH node", body_text="ih")
    inferred_low = await insert_artifact(session, title="INFERRED_LOW node", body_text="il")
    ambiguous = await insert_artifact(session, title="AMBIGUOUS node", body_text="am")
    rejected = await insert_artifact(session, title="REJECTED node", body_text="rj")
    await insert_edge(
        session,
        from_artifact_id=a,
        to_artifact_id=extracted,
        edge_type="calls",
        trust_class="EXTRACTED",
    )
    await insert_edge(
        session,
        from_artifact_id=a,
        to_artifact_id=inferred_high,
        edge_type="documents",
        trust_class="INFERRED_HIGH",
    )
    await insert_edge(
        session,
        from_artifact_id=a,
        to_artifact_id=inferred_low,
        edge_type="documents",
        trust_class="INFERRED_LOW",
    )
    await insert_edge(
        session,
        from_artifact_id=a,
        to_artifact_id=ambiguous,
        edge_type="documents",
        trust_class="AMBIGUOUS",
    )
    await insert_edge(
        session,
        from_artifact_id=a,
        to_artifact_id=rejected,
        edge_type="documents",
        trust_class="REJECTED",
    )
    return {
        "a": a,
        "extracted": extracted,
        "inferred_high": inferred_high,
        "inferred_low": inferred_low,
        "ambiguous": ambiguous,
        "rejected": rejected,
    }


async def test_default_trust_floor_returns_only_extracted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        nodes = await _seed_trust_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_neighbors(
        deps, GetNeighborsRequest(artifact_id=nodes["a"], depth=1), REQUESTER
    )

    by_id = {n.artifact_id: n for n in response.neighbors}
    assert set(by_id) == {nodes["extracted"]}
    neighbor = by_id[nodes["extracted"]]
    assert neighbor.trust_class == "EXTRACTED"
    assert neighbor.claim_supporting is True


async def test_include_inferred_surfaces_inferred_as_non_claim_support(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        nodes = await _seed_trust_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_neighbors(
        deps,
        GetNeighborsRequest(artifact_id=nodes["a"], depth=1, include_inferred=True),
        REQUESTER,
    )

    by_id = {n.artifact_id: n for n in response.neighbors}
    # EXTRACTED plus both INFERRED_* surface; AMBIGUOUS/REJECTED still excluded.
    assert set(by_id) == {nodes["extracted"], nodes["inferred_high"], nodes["inferred_low"]}
    assert by_id[nodes["extracted"]].claim_supporting is True
    # INFERRED_* are routing hints only — never claim-supporting.
    assert by_id[nodes["inferred_high"]].trust_class == "INFERRED_HIGH"
    assert by_id[nodes["inferred_high"]].claim_supporting is False
    assert by_id[nodes["inferred_low"]].claim_supporting is False


async def test_ambiguous_and_rejected_never_returned(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        nodes = await _seed_trust_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    # Lowest possible floor + include_inferred: AMBIGUOUS/REJECTED still excluded.
    response = await get_neighbors(
        deps,
        GetNeighborsRequest(
            artifact_id=nodes["a"],
            depth=1,
            trust_floor="INFERRED_LOW",
            include_inferred=True,
        ),
        REQUESTER,
    )

    returned = {n.artifact_id for n in response.neighbors}
    assert nodes["ambiguous"] not in returned
    assert nodes["rejected"] not in returned


async def test_inferred_only_admitted_when_include_inferred(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        nodes = await _seed_trust_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    # trust_floor below EXTRACTED but include_inferred default False ⇒ still
    # only EXTRACTED (the gate, not just the floor, governs INFERRED_*).
    response = await get_neighbors(
        deps,
        GetNeighborsRequest(artifact_id=nodes["a"], depth=1, trust_floor="INFERRED_HIGH"),
        REQUESTER,
    )

    assert {n.artifact_id for n in response.neighbors} == {nodes["extracted"]}


async def test_trust_and_acl_filters_compose(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A restricted EXTRACTED neighbor is suppressed by ACL even though it
    clears the trust floor — both filters must pass."""
    async with factory() as session:
        a = await insert_artifact(session, title="root", body_text="a", artifact_type="code_chunk")
        # EXTRACTED but team-restricted: requester (no teams) cannot see it.
        restricted = await insert_artifact(
            session, title="restricted", body_text="r", acl_teams=["secret-team"]
        )
        # EXTRACTED and org-public: visible.
        public = await insert_artifact(session, title="public", body_text="p")
        await insert_edge(
            session,
            from_artifact_id=a,
            to_artifact_id=restricted,
            edge_type="calls",
            trust_class="EXTRACTED",
        )
        await insert_edge(
            session,
            from_artifact_id=a,
            to_artifact_id=public,
            edge_type="calls",
            trust_class="EXTRACTED",
        )
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_neighbors(deps, GetNeighborsRequest(artifact_id=a, depth=1), REQUESTER)

    returned = {n.artifact_id for n in response.neighbors}
    assert public in returned
    assert restricted not in returned  # trust passes, ACL does not ⇒ excluded
