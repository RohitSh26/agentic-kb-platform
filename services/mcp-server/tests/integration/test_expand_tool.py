"""context.expand: trust-tiered BFS expansion from seed artifact ids.

DB-backed integration tests covering:
- Seeds reach EXTRACTED neighbors (defined_in / calls / imports) within budget;
  the returned cards include the file a symbol is defined in.
- default trust_floor=EXTRACTED, include_inferred=False returns NO inferred-only
  neighbor; include_inferred=True adds INFERRED neighbors but NEVER an AMBIGUOUS
  edge's neighbor.
- Budget cap: tiny budget_tokens truncates (truncated=True) and never exceeds
  budget.
- A retrieval_event row is written with tool_name="context.expand".
- ACL: an artifact the requester is not authorized for is not returned.
- Schema / registry round-trips (tool registered + handler wired).
- context_pack_id: expansion charges the pack and registers new cards.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker import expand as expand_module
from agentic_mcp_server.context_broker.expand import expand
from agentic_mcp_server.context_broker.state import EvidencePackState, new_pack_id
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS
from agentic_mcp_server.mcp.tool_schemas.context import ExpandRequest

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "expand-agent"
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_code_graph(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """symbol A -defined_in-> file B, A -calls-> C, A -imports-> D (all EXTRACTED)."""
    symbol_a = await insert_artifact(
        session, title="symbol_A", body_text="def foo(): ...", artifact_type="code_symbol"
    )
    file_b = await insert_artifact(
        session, title="file_B.py", body_text="# file B content", artifact_type="code_file"
    )
    func_c = await insert_artifact(
        session, title="func_C", body_text="def bar(): ...", artifact_type="code_symbol"
    )
    dep_d = await insert_artifact(
        session, title="dep_D", body_text="import dep_d", artifact_type="code_symbol"
    )
    await insert_edge(
        session,
        from_artifact_id=symbol_a,
        to_artifact_id=file_b,
        edge_type="defined_in",
        trust_class="EXTRACTED",
    )
    await insert_edge(
        session,
        from_artifact_id=symbol_a,
        to_artifact_id=func_c,
        edge_type="calls",
        trust_class="EXTRACTED",
    )
    await insert_edge(
        session,
        from_artifact_id=symbol_a,
        to_artifact_id=dep_d,
        edge_type="imports",
        trust_class="EXTRACTED",
    )
    return symbol_a, file_b, func_c, dep_d


# ---------------------------------------------------------------------------
# Happy-path: EXTRACTED neighbors including the defining file
# ---------------------------------------------------------------------------


async def test_expand_from_seed_reaches_extracted_neighbors(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        symbol_a, file_b, func_c, dep_d = await _seed_code_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await expand(
        deps,
        ExpandRequest(seed_artifact_ids=[symbol_a], budget_tokens=10_000),
        REQUESTER,
    )

    returned_ids = {c.artifact_id for c in response.cards}
    # seed itself is returned plus all EXTRACTED neighbors
    assert file_b in returned_ids, "defining file must be reachable via defined_in edge"
    assert func_c in returned_ids
    assert dep_d in returned_ids
    assert not response.truncated
    assert response.tokens_used > 0


async def test_expand_returns_seed_itself(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seeds that pass the ACL are included as cards first."""
    async with factory() as session:
        symbol_a, _file_b, *_ = await _seed_code_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await expand(
        deps,
        ExpandRequest(seed_artifact_ids=[symbol_a], budget_tokens=10_000),
        REQUESTER,
    )

    assert symbol_a in {c.artifact_id for c in response.cards}


# ---------------------------------------------------------------------------
# Trust-floor / include_inferred tests
# ---------------------------------------------------------------------------


async def _seed_mixed_trust_graph(
    session: AsyncSession,
) -> dict[str, uuid.UUID]:
    """seed -> extracted_neighbor (EXTRACTED), seed -> inferred_neighbor (INFERRED_HIGH),
    seed -> ambiguous_neighbor (AMBIGUOUS)."""
    seed = await insert_artifact(session, title="seed", body_text="seed content")
    extracted = await insert_artifact(session, title="extracted_neighbor", body_text="extracted")
    inferred = await insert_artifact(session, title="inferred_neighbor", body_text="inferred")
    ambiguous = await insert_artifact(session, title="ambiguous_neighbor", body_text="ambiguous")

    await insert_edge(
        session,
        from_artifact_id=seed,
        to_artifact_id=extracted,
        edge_type="calls",
        trust_class="EXTRACTED",
    )
    await insert_edge(
        session,
        from_artifact_id=seed,
        to_artifact_id=inferred,
        edge_type="documents",
        trust_class="INFERRED_HIGH",
    )
    await insert_edge(
        session,
        from_artifact_id=seed,
        to_artifact_id=ambiguous,
        edge_type="documents",
        trust_class="AMBIGUOUS",
    )
    return {
        "seed": seed,
        "extracted": extracted,
        "inferred": inferred,
        "ambiguous": ambiguous,
    }


async def test_default_trust_floor_no_inferred_neighbor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Default trust_floor=EXTRACTED, include_inferred=False: INFERRED neighbors
    must NOT appear in results."""
    async with factory() as session:
        nodes = await _seed_mixed_trust_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await expand(
        deps,
        ExpandRequest(seed_artifact_ids=[nodes["seed"]], budget_tokens=10_000),
        REQUESTER,
    )

    returned_ids = {c.artifact_id for c in response.cards}
    assert nodes["extracted"] in returned_ids
    assert nodes["inferred"] not in returned_ids
    assert nodes["ambiguous"] not in returned_ids


async def test_include_inferred_adds_inferred_but_not_ambiguous(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """include_inferred=True adds INFERRED neighbors; AMBIGUOUS is still excluded."""
    async with factory() as session:
        nodes = await _seed_mixed_trust_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await expand(
        deps,
        ExpandRequest(
            seed_artifact_ids=[nodes["seed"]],
            include_inferred=True,
            budget_tokens=10_000,
        ),
        REQUESTER,
    )

    returned_ids = {c.artifact_id for c in response.cards}
    assert nodes["extracted"] in returned_ids
    assert nodes["inferred"] in returned_ids
    assert nodes["ambiguous"] not in returned_ids


async def test_ambiguous_and_rejected_never_returned_even_with_include_inferred(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        seed = await insert_artifact(session, title="seed", body_text="s")
        amb = await insert_artifact(session, title="ambiguous", body_text="a")
        rej = await insert_artifact(session, title="rejected", body_text="r")
        await insert_edge(
            session,
            from_artifact_id=seed,
            to_artifact_id=amb,
            edge_type="documents",
            trust_class="AMBIGUOUS",
        )
        await insert_edge(
            session,
            from_artifact_id=seed,
            to_artifact_id=rej,
            edge_type="documents",
            trust_class="REJECTED",
        )
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await expand(
        deps,
        ExpandRequest(
            seed_artifact_ids=[seed],
            include_inferred=True,
            trust_floor="INFERRED_LOW",
            budget_tokens=10_000,
        ),
        REQUESTER,
    )

    returned_ids = {c.artifact_id for c in response.cards}
    assert amb not in returned_ids
    assert rej not in returned_ids


# ---------------------------------------------------------------------------
# Budget cap
# ---------------------------------------------------------------------------


async def test_budget_cap_truncates(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tiny budget_tokens causes truncated=True and tokens_used <= budget."""
    async with factory() as session:
        symbol_a, *_ = await _seed_code_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    # budget of 1 token: should be exceeded almost immediately
    response = await expand(
        deps,
        ExpandRequest(seed_artifact_ids=[symbol_a], budget_tokens=1),
        REQUESTER,
    )

    assert response.truncated is True
    assert response.tokens_used <= 1


async def test_budget_not_exceeded(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        symbol_a, *_ = await _seed_code_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    budget = 50
    response = await expand(
        deps,
        ExpandRequest(seed_artifact_ids=[symbol_a], budget_tokens=budget),
        REQUESTER,
    )

    assert response.tokens_used <= budget


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


async def test_retrieval_event_written_with_correct_tool_name(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        symbol_a, *_ = await _seed_code_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())
    run_id = "-"  # no-pack path uses the sentinel

    await expand(
        deps,
        ExpandRequest(seed_artifact_ids=[symbol_a], budget_tokens=10_000),
        REQUESTER,
    )

    async with factory() as session:
        rows = await fetch_ledger_rows(session, run_id)
    tool_names = [r.tool_name for r in rows]
    assert "context.expand" in tool_names
    expand_row = next(r for r in rows if r.tool_name == "context.expand")
    assert expand_row.status == "approved"
    assert expand_row.agent_name == SUBJECT


# ---------------------------------------------------------------------------
# ACL
# ---------------------------------------------------------------------------


async def test_acl_restricts_unauthorized_artifact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        seed = await insert_artifact(session, title="seed", body_text="seed content")
        restricted = await insert_artifact(
            session,
            title="restricted_neighbor",
            body_text="secret",
            acl_teams=["secret-team"],
        )
        public = await insert_artifact(session, title="public_neighbor", body_text="public content")
        await insert_edge(
            session,
            from_artifact_id=seed,
            to_artifact_id=restricted,
            edge_type="calls",
            trust_class="EXTRACTED",
        )
        await insert_edge(
            session,
            from_artifact_id=seed,
            to_artifact_id=public,
            edge_type="calls",
            trust_class="EXTRACTED",
        )
    deps = make_broker_deps(factory, FakeSearchClient())
    requester_no_teams = Requester(subject=SUBJECT, teams=frozenset())

    response = await expand(
        deps,
        ExpandRequest(seed_artifact_ids=[seed], budget_tokens=10_000),
        requester_no_teams,
    )

    returned_ids = {c.artifact_id for c in response.cards}
    assert public in returned_ids
    assert restricted not in returned_ids


async def test_acl_restricts_seed_itself(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A seed the requester cannot see returns no cards for that seed or its neighbors."""
    async with factory() as session:
        restricted_seed = await insert_artifact(
            session,
            title="restricted_seed",
            body_text="secret seed",
            acl_teams=["secret-team"],
        )
        neighbor = await insert_artifact(session, title="neighbor", body_text="neighbor")
        await insert_edge(
            session,
            from_artifact_id=restricted_seed,
            to_artifact_id=neighbor,
            edge_type="calls",
            trust_class="EXTRACTED",
        )
    deps = make_broker_deps(factory, FakeSearchClient())
    requester_no_teams = Requester(subject=SUBJECT, teams=frozenset())

    response = await expand(
        deps,
        ExpandRequest(seed_artifact_ids=[restricted_seed], budget_tokens=10_000),
        requester_no_teams,
    )

    returned_ids = {c.artifact_id for c in response.cards}
    assert restricted_seed not in returned_ids
    # neighbor is reachable only via the restricted seed; since the seed fails
    # ACL, the BFS starting from it doesn't expand (no allowed seeds = empty frontier)
    assert neighbor not in returned_ids


# ---------------------------------------------------------------------------
# Schema / registry round-trip
# ---------------------------------------------------------------------------


def test_expand_is_registered_in_tool_schemas() -> None:
    assert "context.expand" in TOOL_SCHEMAS
    schema = TOOL_SCHEMAS["context.expand"]
    assert schema.request is ExpandRequest
    from agentic_mcp_server.mcp.tool_schemas.context import ExpandResponse

    assert schema.response is ExpandResponse


def test_expand_request_schema_forbids_empty_seed_list() -> None:
    """min_length=1 on seed_artifact_ids rejects an empty list at the schema layer."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ExpandRequest(seed_artifact_ids=[], budget_tokens=1000)


# ---------------------------------------------------------------------------
# Pack integration: charge + register cards
# ---------------------------------------------------------------------------


async def test_expand_with_pack_charges_budget_and_registers_cards(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        symbol_a, *_ = await _seed_code_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    run_id = "expand-pack-run"
    pack = EvidencePackState(
        context_pack_id=new_pack_id(),
        run_id=run_id,
        kb_version=KB_VERSION,
        build_seq=1,
        retrieval_profile="default",
        summary="test pack",
        budget_tokens=10_000,
        used_run_tokens=0,
        cards={},
        open_questions=[],
    )
    deps.packs.create(pack)

    response = await expand(
        deps,
        ExpandRequest(
            seed_artifact_ids=[symbol_a],
            budget_tokens=10_000,
            context_pack_id=pack.context_pack_id,
        ),
        REQUESTER,
    )

    assert response.tokens_used > 0
    # Pack budget was charged
    assert pack.used_run_tokens == response.tokens_used
    # Cards are registered into the pack by evidence_id
    for card in response.cards:
        assert card.evidence_id in pack.cards

    # Ledger row uses the pack's run_id
    async with factory() as session:
        rows = await fetch_ledger_rows(session, run_id)
    expand_rows = [r for r in rows if r.tool_name == "context.expand"]
    assert len(expand_rows) == 1
    assert expand_rows[0].status == "approved"


async def test_expand_with_pack_skips_seeds_already_in_pack(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seeds whose artifact_id already exists in the pack are not returned again."""
    async with factory() as session:
        symbol_a, file_b, *_ = await _seed_code_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())
    from agentic_mcp_server.mcp.tool_schemas.evidence import EvidenceCard

    existing_card = EvidenceCard(
        evidence_id=str(symbol_a),
        artifact_id=symbol_a,
        level="L1",
        card_type="code_symbol",
        title="symbol_A",
        summary="already in pack",
        confidence=1.0,
        authority_score=0.8,
        tokens_if_expanded=10,
    )
    pack = EvidencePackState(
        context_pack_id=new_pack_id(),
        run_id="skip-seed-run",
        kb_version=KB_VERSION,
        build_seq=1,
        retrieval_profile="default",
        summary="test pack",
        budget_tokens=10_000,
        used_run_tokens=0,
        cards={existing_card.evidence_id: existing_card},
        open_questions=[],
    )
    deps.packs.create(pack)

    response = await expand(
        deps,
        ExpandRequest(
            seed_artifact_ids=[symbol_a],
            budget_tokens=10_000,
            context_pack_id=pack.context_pack_id,
        ),
        REQUESTER,
    )

    returned_ids = {c.artifact_id for c in response.cards}
    # symbol_a is already in the pack, so it must not appear in new cards
    assert symbol_a not in returned_ids
    # But its neighbors (file_b) should still be reachable
    assert file_b in returned_ids


async def test_expand_ledger_crash_refunds_the_pack_charge_and_writes_no_row(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same crash-refund guarantee as open_evidence/request_more/kb_search: an
    unexpected mid-flight ledger failure must refund the pack's token charge
    and the new cards this call would have registered, write no ledger row
    itself, and still propagate (Fix: pack-scoped crash refunds, kb_search's
    precedent commit 346c2d2)."""
    async with factory() as session:
        symbol_a, *_ = await _seed_code_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    run_id = "expand-crash-run"
    pack = EvidencePackState(
        context_pack_id=new_pack_id(),
        run_id=run_id,
        kb_version=KB_VERSION,
        build_seq=1,
        retrieval_profile="default",
        summary="test pack",
        budget_tokens=10_000,
        used_run_tokens=0,
        cards={},
        open_questions=[],
    )
    deps.packs.create(pack)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ledger db down")

    monkeypatch.setattr(expand_module, "insert_event", _boom)

    with pytest.raises(RuntimeError, match="ledger db down"):
        await expand(
            deps,
            ExpandRequest(
                seed_artifact_ids=[symbol_a],
                budget_tokens=10_000,
                context_pack_id=pack.context_pack_id,
            ),
            REQUESTER,
        )

    assert pack.used_run_tokens == 0
    assert pack.usage_for(SUBJECT).tokens == 0
    assert pack.cards == {}
    async with factory() as session:
        rows = await fetch_ledger_rows(session, run_id)
    assert rows == []

    # the refund actually restores the budget: a working call afterwards still
    # charges normally, not against an already-inflated used_run_tokens
    monkeypatch.undo()
    recovered = await expand(
        deps,
        ExpandRequest(
            seed_artifact_ids=[symbol_a],
            budget_tokens=10_000,
            context_pack_id=pack.context_pack_id,
        ),
        REQUESTER,
    )
    assert recovered.tokens_used > 0
    assert pack.used_run_tokens == recovered.tokens_used
    async with factory() as session:
        rows = await fetch_ledger_rows(session, run_id)
    assert [row.tool_name for row in rows] == ["context.expand"]


# ---------------------------------------------------------------------------
# No active kb_version
# ---------------------------------------------------------------------------


async def test_no_active_kb_version_raises_tool_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import text

    async with factory() as session:
        await session.execute(text("DELETE FROM kb_build_run"))
        await session.commit()
    deps = make_broker_deps(factory, FakeSearchClient())

    with pytest.raises(ToolError, match="no active kb_version"):
        await expand(
            deps,
            ExpandRequest(seed_artifact_ids=[uuid.uuid4()], budget_tokens=1000),
            REQUESTER,
        )


# ---------------------------------------------------------------------------
# Semantic reuse / exact-cache-hit (no double ledger row for same seeds)
# ---------------------------------------------------------------------------


async def test_two_expand_calls_write_two_ledger_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """expand has no cross-call caching — each call writes its own ledger row."""
    async with factory() as session:
        symbol_a, *_ = await _seed_code_graph(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    await expand(deps, ExpandRequest(seed_artifact_ids=[symbol_a], budget_tokens=10_000), REQUESTER)
    await expand(deps, ExpandRequest(seed_artifact_ids=[symbol_a], budget_tokens=10_000), REQUESTER)

    async with factory() as session:
        rows = await fetch_ledger_rows(session, "-")
    expand_rows = [r for r in rows if r.tool_name == "context.expand"]
    assert len(expand_rows) == 2
