"""get_task_context behavior against a real (local) Postgres registry (PR-39).

Covers the acceptance criteria end to end on seeded fixtures: alias-index
resolution with graceful pre-PR-38 degradation, ambiguity surfaced as
candidates + open questions (never a guess), the three name-collision fixtures
all demoted to `interpreted`+caveat, confidence_floor filtering, the
server-side response budget (request cap never an escape hatch), a
retrieval_event per call, requester-team ACL filtering on every returned
artifact, and a measured, printed p50 on the seeded KB.
"""

import json
import time
import uuid
from collections.abc import AsyncIterator
from statistics import median

import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    insert_artifact,
    insert_build_run,
    insert_code_unit,
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
from agentic_mcp_server.context_broker.task_context import get_task_context
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.mcp.tool_schemas.task_context import (
    GetTaskContextRequest,
    GetTaskContextResponse,
    TaskContextHints,
)

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

REQUESTER = Requester(subject="impl-agent", teams=frozenset())
MEMBER = Requester(subject="payments-dev", teams=frozenset({"payments-team"}))


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


def _alias_body(targets: list[tuple[str, uuid.UUID]]) -> str:
    """A PR-38 alias_reference_v1 body (docs/contracts/alias-reference.md)."""
    return json.dumps(
        {
            "schema": "alias_reference_v1",
            "alias": "payment validation",
            "variants": ["payment-validation"],
            "confidence_tier": "interpreted",
            "confirmation_count": len(targets),
            "targets": [
                {"path": path, "artifact_id": str(artifact_id), "count": 1}
                for path, artifact_id in targets
            ],
            "evidence": [],
        }
    )


async def _seed_alias_scope(
    session: AsyncSession,
    search: FakeSearchClient,
    *,
    target_acl: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """One alias row pointing at one code file; returns (alias_id, target_id)."""
    target_id = await insert_artifact(
        session,
        title="payments.py",
        body_text="def validate(amount): ...",
        artifact_type="code_file",
        source_uri="github://org/repo/services/checkout/payments.py",
        acl_teams=target_acl,
    )
    alias_id = await insert_artifact(
        session,
        title="payment validation",
        body_text=_alias_body([("services/checkout/payments.py", target_id)]),
        artifact_type="alias_reference",
        knowledge_kind="interpreted",
        source_uri="github://org/repo/alias/payment-validation",
    )
    search.seed("payment", [SearchHit(artifact_id=alias_id, score=5.0)])
    return alias_id, target_id


def _paths(response: GetTaskContextResponse) -> set[str]:
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


async def _ledger_rows(session: AsyncSession) -> list[tuple[str, str, dict[str, object]]]:
    result = await session.execute(
        text(
            "SELECT tool_name, status, details FROM retrieval_event"
            " ORDER BY created_at, retrieval_id"
        )
    )
    return [(row.tool_name, row.status, row.details or {}) for row in result]


# --------------------------------------------------------------------- scope resolution


async def test_alias_index_resolves_scope_and_writes_an_approved_ledger_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        _, target_id = await _seed_alias_scope(session, search)
    deps = make_broker_deps(factory, search)

    response = await get_task_context(
        deps, GetTaskContextRequest(task_description="fix the payment validation"), REQUESTER
    )

    assert [entity.resolution_source for entity in response.resolved_scope.entities] == [
        "alias_index"
    ]
    entity = response.resolved_scope.entities[0]
    assert entity.entity_id == target_id
    assert entity.path == "services/checkout/payments.py"
    assert entity.confidence_tier == "interpreted"  # alias rows are interpreted at creation
    assert response.resolved_scope.ambiguous_candidates == []
    assert target_id in response.evidence_ids
    assert response.budget_used.calls >= 1
    assert response.budget_used.tokens > 0

    async with factory() as session:
        rows = await _ledger_rows(session)
    assert [(tool, status) for tool, status, _ in rows] == [("get_task_context", "approved")]
    details = rows[0][2]
    assert details["retried"] is False
    assert details["tracing"] is False
    node_latency = details["node_latency_ms"]
    assert isinstance(node_latency, dict)
    assert set(node_latency) >= {
        "resolve_scope",
        "blast_radius",
        "conventions",
        "similar_prior_changes",
        "synthesize",
    }


async def test_kb_predating_pr38_degrades_to_plain_search_resolution(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """No alias_reference rows at all (the KB predates PR-38): the resolver falls
    through to keyword search and still answers, at `interpreted` tier."""
    search = FakeSearchClient()
    async with factory() as session:
        file_id = await insert_artifact(
            session,
            title="payments.py",
            body_text="def validate(amount): ...",
            artifact_type="code_file",
            source_uri="github://org/repo/services/checkout/payments.py",
        )
    search.seed("payment", [SearchHit(artifact_id=file_id, score=3.0)])
    deps = make_broker_deps(factory, search)

    response = await get_task_context(
        deps, GetTaskContextRequest(task_description="fix the payment validation"), REQUESTER
    )

    assert [entity.resolution_source for entity in response.resolved_scope.entities] == ["search"]
    assert response.resolved_scope.entities[0].confidence_tier == "interpreted"


async def test_ambiguous_symbol_hint_returns_candidates_never_a_guess(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The audit's collision shape at resolution time: two same-named definitions.
    The tool must stop and hand back both candidates plus an open question."""
    search = FakeSearchClient()
    async with factory() as session:
        _, symbols_a = await insert_code_unit(
            session,
            source_uri="github://org/repo/services/x/module_a.py",
            symbols={"resolve": "def resolve(): ..."},
        )
        _, symbols_b = await insert_code_unit(
            session,
            source_uri="github://org/repo/services/x/module_b.py",
            symbols={"resolve": "class Resolver:\n    def resolve(self): ..."},
        )
    search.seed(
        "resolve",
        [
            SearchHit(artifact_id=symbols_a["resolve"], score=2.0),
            SearchHit(artifact_id=symbols_b["resolve"], score=1.9),
        ],
    )
    deps = make_broker_deps(factory, search)

    response = await get_task_context(
        deps,
        GetTaskContextRequest(
            task_description="change the resolve helper",
            hints=TaskContextHints(symbols=["resolve"]),
        ),
        REQUESTER,
    )

    assert response.resolved_scope.entities == []
    candidates = response.resolved_scope.ambiguous_candidates
    assert len(candidates) == 1
    assert set(candidates[0].candidates) == {symbols_a["resolve"], symbols_b["resolve"]}
    assert "resolve" in candidates[0].alias_text
    assert any("resolve" in question for question in response.open_questions)
    # ambiguity is an answer: the broadened retry must NOT have fired
    async with factory() as session:
        rows = await _ledger_rows(session)
    assert rows[0][2]["retried"] is False


async def test_competing_alias_phrases_surface_as_ambiguity(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        target_a = await insert_artifact(
            session,
            title="payments.py",
            body_text="def validate(amount): ...",
            artifact_type="code_file",
            source_uri="github://org/repo/services/checkout/payments.py",
        )
        target_b = await insert_artifact(
            session,
            title="payment_rules.py",
            body_text="RULES = [...]",
            artifact_type="code_file",
            source_uri="github://org/repo/services/billing/payment_rules.py",
        )
        alias_a = await insert_artifact(
            session,
            title="payment validation",
            body_text=_alias_body([("services/checkout/payments.py", target_a)]),
            artifact_type="alias_reference",
            knowledge_kind="interpreted",
            source_uri="github://org/repo/alias/payment-validation",
        )
        alias_b = await insert_artifact(
            session,
            title="payment validation rules",
            body_text=_alias_body([("services/billing/payment_rules.py", target_b)]),
            artifact_type="alias_reference",
            knowledge_kind="interpreted",
            source_uri="github://org/repo/alias/payment-validation-rules",
        )
    # comparable relevance (>= 0.8 of the top score) with different targets
    search.seed(
        "payment",
        [
            SearchHit(artifact_id=alias_a, score=5.0),
            SearchHit(artifact_id=alias_b, score=4.5),
        ],
    )
    deps = make_broker_deps(factory, search)

    response = await get_task_context(
        deps, GetTaskContextRequest(task_description="fix the payment validation"), REQUESTER
    )

    assert response.resolved_scope.entities == []
    candidates = response.resolved_scope.ambiguous_candidates
    assert len(candidates) == 1
    assert set(candidates[0].candidates) == {target_a, target_b}
    assert response.open_questions  # the caller is told to disambiguate


# ------------------------------------------------- collision fixtures (blast radius tiering)


async def _seed_collision(
    session: AsyncSession,
    search: FakeSearchClient,
    *,
    import_to: str | None,
    target_defined_in: bool = True,
    target_acl: list[str] | None = None,
) -> dict[str, uuid.UUID]:
    """The audit fixture: handle() in caller.py calls resolve(); module_a.py has the
    free function, module_b.py a same-named method. `import_to` seeds an imports
    edge caller.py -> that module (None = no import corroboration at all)."""
    ids: dict[str, uuid.UUID] = {}
    caller_file, caller_symbols = await insert_code_unit(
        session,
        source_uri="github://org/repo/services/x/caller.py",
        symbols={"handle": "def handle(): return resolve()"},
    )
    ids["caller_file"], ids["handle"] = caller_file, caller_symbols["handle"]
    file_a, symbols_a = await insert_code_unit(
        session,
        source_uri="github://org/repo/services/x/module_a.py",
        symbols={"resolve": "def resolve(): ..."},
        acl_teams=target_acl,
    )
    ids["file_a"], ids["resolve_a"] = file_a, symbols_a["resolve"]
    file_b, symbols_b = await insert_code_unit(
        session,
        source_uri="github://org/repo/services/x/module_b.py",
        symbols={"resolve": "class Resolver:\n    def resolve(self): ..."},
    )
    ids["file_b"], ids["resolve_b"] = file_b, symbols_b["resolve"]

    if not target_defined_in:
        # detach the free function from its module: the corroboration input is gone
        await session.execute(
            text(
                "DELETE FROM knowledge_edge WHERE from_artifact_id = CAST(:sid AS uuid)"
                " AND edge_type = 'defined_in'"
            ),
            {"sid": str(ids["resolve_a"])},
        )
        await session.commit()
    await insert_edge(
        session,
        from_artifact_id=ids["handle"],
        to_artifact_id=ids["resolve_a"],
        edge_type="calls",
    )
    if import_to is not None:
        await insert_edge(
            session,
            from_artifact_id=caller_file,
            to_artifact_id=ids[import_to],
            edge_type="imports",
        )
    search.seed("handle", [SearchHit(artifact_id=ids["handle"], score=3.0)])
    return ids


HANDLE_HINT = GetTaskContextRequest(
    task_description="change what handle does",
    hints=TaskContextHints(symbols=["handle"]),
)


async def test_collision_1_uncorroborated_calls_edge_is_interpreted_with_caveat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_collision(session, search, import_to=None)
    deps = make_broker_deps(factory, search)

    response = await get_task_context(deps, HANDLE_HINT, REQUESTER)

    (callee,) = [e for e in response.blast_radius.callees if e.edge_type == "calls"]
    assert callee.path == "services/x/module_a.py"
    assert callee.confidence_tier == "interpreted"
    assert callee.caveat is not None and "module_a.py" in callee.caveat


async def test_collision_2_import_of_the_wrong_module_is_still_interpreted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An imports edge exists — but to the same-named METHOD's module, not the
    labelled target's. The single confidently-wrong-target audit shape."""
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_collision(session, search, import_to="file_b")
    deps = make_broker_deps(factory, search)

    response = await get_task_context(deps, HANDLE_HINT, REQUESTER)

    (callee,) = [e for e in response.blast_radius.callees if e.edge_type == "calls"]
    assert callee.confidence_tier == "interpreted"
    assert callee.caveat is not None


async def test_collision_3_unresolvable_target_module_is_interpreted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_collision(session, search, import_to=None, target_defined_in=False)
    deps = make_broker_deps(factory, search)

    response = await get_task_context(deps, HANDLE_HINT, REQUESTER)

    (callee,) = [e for e in response.blast_radius.callees if e.edge_type == "calls"]
    assert callee.confidence_tier == "interpreted"
    assert callee.caveat is not None and "could not be resolved" in callee.caveat


async def test_no_collision_fixture_ever_surfaces_as_confident_deterministic(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The brief's 3/3 gate, asserted in one place: across every collision variant,
    no `calls` edge may read `deterministic` or drop its caveat."""
    for variant in (
        {"import_to": None},
        {"import_to": "file_b"},
        {"import_to": None, "target_defined_in": False},
    ):
        search = FakeSearchClient()
        async with make_session_factory()() as session:
            await clean_registry(session)
            await insert_build_run(session, KB_VERSION, "active")
            await _seed_collision(session, search, **variant)  # type: ignore[arg-type]
        deps = make_broker_deps(make_session_factory(), search)
        response = await get_task_context(deps, HANDLE_HINT, REQUESTER)
        calls_edges = [
            entry
            for entry in (*response.blast_radius.callers, *response.blast_radius.callees)
            if entry.edge_type == "calls"
        ]
        assert calls_edges, f"variant {variant}: the calls edge must still be surfaced"
        for entry in calls_edges:
            assert entry.confidence_tier == "interpreted", f"variant {variant}"
            assert entry.caveat, f"variant {variant}: a demoted edge must carry its caveat"


async def test_import_corroborated_call_is_deterministic(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The control: the caller's file imports the target's module — the audit
    rule's corroboration is satisfied and the edge may read deterministic."""
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_collision(session, search, import_to="file_a")
    deps = make_broker_deps(factory, search)

    response = await get_task_context(deps, HANDLE_HINT, REQUESTER)

    (callee,) = [e for e in response.blast_radius.callees if e.edge_type == "calls"]
    assert callee.confidence_tier == "deterministic"
    assert callee.caveat is None


async def test_tests_edges_are_deterministic_ast_facts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        ids = await _seed_collision(session, search, import_to="file_a")
        test_id = await insert_artifact(
            session,
            title="test_caller.py",
            body_text="def test_handle(): ...",
            artifact_type="test",
            source_uri="github://org/repo/tests/x/test_caller.py",
        )
        await insert_edge(
            session, from_artifact_id=test_id, to_artifact_id=ids["handle"], edge_type="tests"
        )
    deps = make_broker_deps(factory, search)

    response = await get_task_context(deps, HANDLE_HINT, REQUESTER)

    (covering_test,) = response.blast_radius.tests
    assert covering_test.entity_id == test_id
    assert covering_test.confidence_tier == "deterministic"
    assert covering_test.caveat is None


# -------------------------------------------------------------------- confidence floor


async def test_confidence_floor_forces_interpreted_content_out(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        ids = await _seed_collision(session, search, import_to=None)  # uncorroborated
        doc_id = await insert_artifact(
            session,
            title="Handler conventions",
            body_text="caller handlers stay thin; module_a owns resolution",
        )
    # one seed per keyword (seed() replaces): the symbol hit AND the convention doc
    search.seed(
        "handle",
        [
            SearchHit(artifact_id=ids["handle"], score=3.0),
            SearchHit(artifact_id=doc_id, score=1.0),
        ],
    )
    deps = make_broker_deps(factory, search)

    floored = await get_task_context(
        deps,
        GetTaskContextRequest(
            task_description="change what handle does",
            hints=TaskContextHints(symbols=["handle"]),
            confidence_floor="deterministic",
        ),
        REQUESTER,
    )

    # the hint-resolved entity is deterministic and survives; every interpreted
    # item (the demoted calls edge, conventions, prior changes) is forced out
    assert [e.confidence_tier for e in floored.resolved_scope.entities] == ["deterministic"]
    assert floored.blast_radius.callees == []
    assert floored.conventions == []
    assert floored.similar_prior_changes == []

    unfloored = await get_task_context(deps, HANDLE_HINT, REQUESTER)
    assert [e.edge_type for e in unfloored.blast_radius.callees] == ["calls"]


# ------------------------------------------------------------------------------ budget


async def test_response_budget_trims_the_tail_and_request_cap_is_clamped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        _, target_id = await _seed_alias_scope(session, search)
        commit_hits = []
        for sha in ("abc123", "def456", "fee789"):
            commit_id = await insert_artifact(
                session,
                title=f"fix(payments): harden validation ({sha})",
                body_text=f"fix(payments): harden validation ({sha})\n\nM payments.py",
                artifact_type="commit",
                source_uri=f"commit:git:{sha}",
            )
            commit_hits.append(SearchHit(artifact_id=commit_id, score=2.0))
        doc_id = await insert_artifact(
            session,
            title="Payments validation conventions",
            body_text="payments validators reject negative amounts and log rejects",
        )
    search.seed("payment", [SearchHit(artifact_id=doc_id, score=1.5), *commit_hits])
    search.seed("validation", [SearchHit(artifact_id=target_id, score=0.1)])

    request = GetTaskContextRequest(task_description="fix the payment validation")
    roomy = await get_task_context(make_broker_deps(factory, search), request, REQUESTER)
    assert roomy.similar_prior_changes and roomy.conventions  # there IS a tail to trim

    # a request max_tokens far above the server cap must NOT widen it (clamp rule)
    tight_settings = BrokerSettings(task_context_max_tokens=230)
    tight = await get_task_context(
        make_broker_deps(factory, search, settings=tight_settings),
        GetTaskContextRequest(
            task_description="fix the payment validation", max_tokens=1_000_000
        ),
        REQUESTER,
    )

    assert tight.budget_used.tokens <= 230
    assert tight.budget_used.tokens < roomy.budget_used.tokens
    # the tail went first; the resolved scope is never trimmed
    assert len(tight.similar_prior_changes) < len(roomy.similar_prior_changes)
    assert tight.resolved_scope.entities == roomy.resolved_scope.entities


# --------------------------------------------------------------------------- ledger + ACL


async def test_no_active_kb_version_errors_and_writes_an_error_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await session.execute(text("DELETE FROM kb_build_run"))
        await session.commit()
    deps = make_broker_deps(factory, FakeSearchClient())

    with pytest.raises(ToolError, match="no active kb_version"):
        await get_task_context(
            deps, GetTaskContextRequest(task_description="anything"), REQUESTER
        )

    async with factory() as session:
        rows = await _ledger_rows(session)
    assert [(tool, status) for tool, status, _ in rows] == [("get_task_context", "error")]


async def test_acl_hides_a_restricted_alias_target_from_outsiders(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        _, target_id = await _seed_alias_scope(session, search, target_acl=["payments-team"])
    deps = make_broker_deps(factory, search)
    request = GetTaskContextRequest(task_description="fix the payment validation")

    outsider = await get_task_context(deps, request, REQUESTER)
    assert "services/checkout/payments.py" not in _paths(outsider)
    assert target_id not in outsider.evidence_ids

    insider = await get_task_context(deps, request, MEMBER)
    assert [entity.entity_id for entity in insider.resolved_scope.entities] == [target_id]


async def test_acl_drops_a_restricted_blast_neighbor_before_it_reveals_connectivity(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_collision(
            session, search, import_to="file_a", target_acl=["payments-team"]
        )
    deps = make_broker_deps(factory, search)

    outsider = await get_task_context(deps, HANDLE_HINT, REQUESTER)
    assert outsider.blast_radius.callees == []
    assert "services/x/module_a.py" not in _paths(outsider)

    insider = await get_task_context(deps, HANDLE_HINT, MEMBER)
    assert [entry.path for entry in insider.blast_radius.callees] == ["services/x/module_a.py"]


# ------------------------------------------------------------------------------- perf


async def test_p50_on_a_seeded_kb_is_measured_and_printed(
    factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Measures the whole tool path (graph + ledger) on seeded fixtures. The PR-39
    brief's target is < 2s (reported, not asserted); the loose gate here is < 5s."""
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_collision(session, search, import_to="file_a")
        _, target_id = await _seed_alias_scope(session, search)
    search.seed("validation", [SearchHit(artifact_id=target_id, score=0.5)])
    deps = make_broker_deps(factory, search)
    request = GetTaskContextRequest(
        task_description="change what handle does to the payment validation",
        hints=TaskContextHints(symbols=["handle"]),
    )

    durations: list[float] = []
    for _ in range(11):
        started = time.monotonic()
        response = await get_task_context(deps, request, REQUESTER)
        durations.append(time.monotonic() - started)
        assert response.resolved_scope.entities  # every timed call did real work

    p50 = median(durations)
    with capsys.disabled():
        print(
            f"\nget_task_context p50={p50 * 1000:.1f}ms "
            f"(min={min(durations) * 1000:.1f}ms max={max(durations) * 1000:.1f}ms, "
            f"n={len(durations)}, target <2s per PR-39 brief, gate <5s)"
        )
    assert p50 < 5.0
