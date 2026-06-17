"""context.create_change_pack against a real (local) Postgres registry + code graph.

Proves the BUILD-lane selector does what grep can't: it resolves the TARGET file from
an exact symbol hint, pulls the TEST file via a `tests` edge WITHOUT the task naming it,
adds only graph-connected DEPENDENCY files (capped), leaves unrelated runtime files out,
and writes a retrieval_event for the selection.
"""

from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    fetch_ledger_rows,
    insert_artifact,
    insert_build_run,
    insert_code_unit,
    insert_edge,
    make_broker_deps,
    require_registry_schema,
)
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker import change_context
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.mcp.tool_schemas.change import ChangeContextRequest

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

ACTIVE_SEQ = 5
_REQUESTER = Requester(subject="agent-impl", teams=frozenset())

_TARGET_URI = "github://acme/repo/src/pkg/connectors/github_rest.py"
_DEP_URI = "github://acme/repo/src/pkg/http_client.py"
_TEST_URI = "github://acme/repo/tests/unit/test_github_rest_backend.py"
_NOISE_URI = "github://acme/repo/src/pkg/wikify.py"

_TARGET_PATH = "src/pkg/connectors/github_rest.py"
_DEP_PATH = "src/pkg/http_client.py"
_TEST_PATH = "tests/unit/test_github_rest_backend.py"
_NOISE_PATH = "src/pkg/wikify.py"


@pytest.fixture()
def factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory()


@pytest.fixture(autouse=True)
async def registry(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
        await insert_build_run(session, KB_VERSION, "active", build_seq=ACTIVE_SEQ)
    yield


async def test_change_pack_resolves_target_test_and_deps_from_the_graph(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        # target file + its symbol; the symbol body USES the imported HttpFetchError/AsyncHttpClient
        target_file, target_syms = await insert_code_unit(
            session,
            source_uri=_TARGET_URI,
            symbols={
                "GithubRestBackend": (
                    "class GithubRestBackend:\n"
                    "    def fetch(self, client: AsyncHttpClient):\n"
                    "        raise HttpFetchError('boom')"
                )
            },
        )
        # the imported dependency file defines the exception + client contract
        dep_file, _ = await insert_code_unit(
            session,
            source_uri=_DEP_URI,
            symbols={
                "HttpFetchError": "class HttpFetchError(Exception): ...",
                "AsyncHttpClient": "class AsyncHttpClient: ...",
            },
        )
        test_sym = await insert_artifact(
            session,
            title="test_fetch_retries",
            body_text="def test_fetch_retries(): GithubRestBackend().fetch()",
            artifact_type="test",
            source_type="github_code",
            source_uri=_TEST_URI,
        )
        noise = await insert_artifact(
            session,
            title="WikifyService",
            body_text="class WikifyService: ...",
            artifact_type="code_symbol",
            source_type="github_code",
            source_uri=_NOISE_URI,
        )
        # target file --imports--> dependency file ; test --tests--> target symbol
        await insert_edge(
            session,
            from_artifact_id=target_file,
            to_artifact_id=dep_file,
            edge_type="imports",
            confidence=1.0,
        )
        await insert_edge(
            session,
            from_artifact_id=test_sym,
            to_artifact_id=target_syms["GithubRestBackend"],
            edge_type="tests",
            confidence=0.9,
        )

    # Search returns the target symbol + noise; the resolver — not search — must pick.
    search = FakeSearchClient()
    search.seed(
        "retry",
        [
            SearchHit(artifact_id=target_syms["GithubRestBackend"], score=0.9),
            SearchHit(artifact_id=noise, score=0.5),
        ],
    )
    deps = make_broker_deps(factory, search)

    # The task names the SYMBOL and the source file — never the test file.
    request = ChangeContextRequest(task="Add a retry path to GithubRestBackend in github_rest.py")
    resp = await change_context.create_change_pack(deps, request, _REQUESTER)

    # target: resolved from the exact symbol hint, est_tokens populated, with a reason
    assert [f.path for f in resp.target_files] == [_TARGET_PATH]
    target = resp.target_files[0]
    assert target.confidence >= 0.9
    assert "GithubRestBackend" in target.reason
    assert target.est_tokens > 0

    # test: resolved via the `tests` edge though the task never named the test file
    test_paths = [f.path for f in resp.test_files]
    assert _TEST_PATH in test_paths
    assert _TEST_PATH not in request.task

    # dependency: the imported file defining the contract the change uses, named in the reason
    dep_paths = [f.path for f in resp.dependency_files]
    assert dep_paths == [_DEP_PATH]
    assert len(resp.dependency_files) <= 2
    assert "HttpFetchError" in resp.dependency_files[0].reason

    # the unrelated runtime file is never selected anywhere
    all_paths = {f.path for f in resp.target_files + resp.test_files + resp.dependency_files}
    assert _NOISE_PATH not in all_paths
    assert "GithubRestBackend" in resp.relevant_symbols

    # a retrieval_event records the selection (run_id "-" is the BUILD-lane sentinel)
    async with factory() as session:
        rows = await fetch_ledger_rows(session, "-")
    assert [r.tool_name for r in rows] == ["context.create_change_pack"]
    assert rows[0].agent_name == "agent-impl"


async def test_change_pack_ranks_used_client_above_ubiquitous_logger(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression for a real BUILD-lane failure: the target imports BOTH a domain client it
    actually USES (``AsyncHttpClient`` in ``http_client.py``) and the ubiquitous logging utility
    (``structured_logging.py``). The selector must surface the USED client as the dependency —
    not the logger every file imports — so the implementer sees the real ``AsyncHttpClient`` API.
    """
    log_uri = "github://acme/repo/src/pkg/structured_logging.py"
    log_path = "src/pkg/structured_logging.py"
    async with factory() as session:
        # target uses BOTH the http client AND a logger from the body
        target_file, target_syms = await insert_code_unit(
            session,
            source_uri=_TARGET_URI,
            symbols={
                "GitHubRestBackend": (
                    "class GitHubRestBackend:\n"
                    "    def repo_is_accessible(self, client: AsyncHttpClient):\n"
                    "        get_logger(__name__).info('checking')\n"
                    "        return client.request('GET', f'/repos/{owner}/{repo}')"
                )
            },
        )
        # the genuinely-needed dependency: defines the AsyncHttpClient the target calls
        dep_file, _ = await insert_code_unit(
            session,
            source_uri=_DEP_URI,
            symbols={"AsyncHttpClient": "class AsyncHttpClient:\n    def request(self): ..."},
        )
        # the ubiquitous utility: imported by the target and USED (get_logger) but task-irrelevant
        log_file, _ = await insert_code_unit(
            session,
            source_uri=log_uri,
            symbols={"get_logger": "def get_logger(name): ..."},
        )
        for imported in (dep_file, log_file):
            await insert_edge(
                session,
                from_artifact_id=target_file,
                to_artifact_id=imported,
                edge_type="imports",
                confidence=1.0,
            )
    search = FakeSearchClient()
    search.seed("get", [SearchHit(artifact_id=target_syms["GitHubRestBackend"], score=0.9)])
    deps = make_broker_deps(factory, search)

    request = ChangeContextRequest(
        task=(
            "Add a method repo_is_accessible(self, client) to GitHubRestBackend that does a "
            "GET /repos/{owner}/{repo} and returns True on 200, False on 404, with a unit test."
        )
    )
    resp = await change_context.create_change_pack(deps, request, _REQUESTER)

    dep_paths = [f.path for f in resp.dependency_files]
    # the used client is selected; the ubiquitous logger does NOT outrank it
    assert _DEP_PATH in dep_paths
    assert dep_paths.index(_DEP_PATH) == 0, dep_paths
    if log_path in dep_paths:  # if present at all, it must rank BELOW the used client
        assert dep_paths.index(log_path) > dep_paths.index(_DEP_PATH)
    assert "AsyncHttpClient" in resp.dependency_files[0].reason


async def test_change_pack_scopes_tests_and_deps_to_the_targets_repo(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression for a real BUILD-lane failure: a kb-builder target resolved an mcp-server TEST
    purely on lexical overlap. The resolver MUST scope test + dependency candidates to the
    target's own ``services/<name>`` boundary, so a kb-builder target only ever resolves
    kb-builder tests/deps — never a cross-service file that merely mentions the same symbols."""
    target_uri = (
        "file:///repo/services/kb-builder/src/agentic_kb_builder/connectors/github_rest.py"
    )
    target_path = "/repo/services/kb-builder/src/agentic_kb_builder/connectors/github_rest.py"
    same_service_test_uri = (
        "file:///repo/services/kb-builder/tests/unit/test_github_rest_backend.py"
    )
    same_service_test_path = "/repo/services/kb-builder/tests/unit/test_github_rest_backend.py"
    # the genuine same-service dependency the target imports
    dep_uri = "file:///repo/services/kb-builder/src/agentic_kb_builder/http_client.py"
    dep_path = "/repo/services/kb-builder/src/agentic_kb_builder/http_client.py"
    # the TEMPTING cross-service file: a DIFFERENT service that lexically mentions the same names
    cross_service_uri = (
        "file:///repo/services/mcp-server/tests/integration/test_create_change_pack.py"
    )
    cross_service_path = "/repo/services/mcp-server/tests/integration/test_create_change_pack.py"

    async with factory() as session:
        target_file, target_syms = await insert_code_unit(
            session,
            source_uri=target_uri,
            symbols={
                "AsyncHttpClient": (
                    "class AsyncHttpClient:\n"
                    "    def request(self): raise HttpFetchError('boom')"
                )
            },
        )
        dep_file, _ = await insert_code_unit(
            session,
            source_uri=dep_uri,
            symbols={
                "HttpFetchError": "class HttpFetchError(Exception): ...",
                "AsyncHttpClient": "class AsyncHttpClient: ...",
            },
        )
        # a cross-service NON-test dependency that defines a same-named contract symbol — the
        # tempting cross-service dep that the repo-scope guard must drop from stage 4.
        cross_dep_uri = (
            "file:///repo/services/mcp-server/src/agentic_mcp_server/http_client.py"
        )
        cross_dep_path = "/repo/services/mcp-server/src/agentic_mcp_server/http_client.py"
        cross_dep_file, _ = await insert_code_unit(
            session,
            source_uri=cross_dep_uri,
            symbols={"HttpFetchError": "class HttpFetchError(Exception): ..."},
        )
        # same-service test linked by a `tests` edge to the target symbol
        same_service_test = await insert_artifact(
            session,
            title="test_async_http_client",
            body_text="def test_async_http_client(): AsyncHttpClient().request()",
            artifact_type="test",
            source_type="github_code",
            source_uri=same_service_test_uri,
        )
        # cross-service file in a DIFFERENT service that mentions the very same symbols (lexical
        # bait). Its body references AsyncHttpClient + HttpFetchError + structured_logging. It is
        # given BOTH a `tests` edge (so it reaches stage 3) AND an `imports` edge (so it reaches
        # stage 4) — the repo-scope guard, not lack of a path, is what must exclude it.
        cross_service_test, cross_syms = await insert_code_unit(
            session,
            source_uri=cross_service_uri,
            symbols={
                "test_create_change_pack": (
                    "def test_create_change_pack(): AsyncHttpClient(); HttpFetchError()"
                )
            },
        )
        await insert_edge(
            session,
            from_artifact_id=target_file,
            to_artifact_id=dep_file,
            edge_type="imports",
            confidence=1.0,
        )
        # the target file ALSO (wrongly, per the bug) imports the cross-service files
        await insert_edge(
            session,
            from_artifact_id=target_file,
            to_artifact_id=cross_service_test,
            edge_type="imports",
            confidence=1.0,
        )
        await insert_edge(
            session,
            from_artifact_id=target_file,
            to_artifact_id=cross_dep_file,
            edge_type="imports",
            confidence=1.0,
        )
        await insert_edge(
            session,
            from_artifact_id=same_service_test,
            to_artifact_id=target_syms["AsyncHttpClient"],
            edge_type="tests",
            confidence=0.9,
        )
        await insert_edge(
            session,
            from_artifact_id=cross_syms["test_create_change_pack"],
            to_artifact_id=target_syms["AsyncHttpClient"],
            edge_type="tests",
            confidence=0.9,
        )

    search = FakeSearchClient()
    search.seed(
        "retry",
        [SearchHit(artifact_id=target_syms["AsyncHttpClient"], score=0.9)],
    )
    deps = make_broker_deps(factory, search)

    request = ChangeContextRequest(
        task="Add a retry path to AsyncHttpClient in github_rest.py"
    )
    resp = await change_context.create_change_pack(deps, request, _REQUESTER)

    assert [f.path for f in resp.target_files] == [target_path]
    # the same-service test resolves; the cross-service test is dropped despite lexical overlap
    test_paths = [f.path for f in resp.test_files]
    assert same_service_test_path in test_paths
    assert cross_service_path not in test_paths

    dep_paths = [f.path for f in resp.dependency_files]
    assert dep_path in dep_paths
    assert cross_dep_path not in dep_paths

    # NO cross-service path appears ANYWHERE in the pack — not as a test and not as a dependency
    all_paths = {
        f.path for f in resp.target_files + resp.test_files + resp.dependency_files
    }
    assert cross_service_path not in all_paths
    assert cross_dep_path not in all_paths
    assert all("services/kb-builder/" in p for p in all_paths), all_paths


async def test_change_pack_caps_dependency_files(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three+ connected dependency files must be capped to the top _MAX_DEPENDENCY_FILES, so the
    token win is not lost to a large fan-out of imported modules."""
    async with factory() as session:
        target_file, target_syms = await insert_code_unit(
            session,
            source_uri=_TARGET_URI,
            symbols={"GithubRestBackend": "class GithubRestBackend: ..."},
        )
        for i in range(4):  # each imported file defines a contract type (Error) -> scores > 0
            dep_file, _ = await insert_code_unit(
                session,
                source_uri=f"github://acme/repo/src/pkg/dep{i}.py",
                symbols={f"Dep{i}Error": f"class Dep{i}Error(Exception): ..."},
            )
            await insert_edge(
                session,
                from_artifact_id=target_file,
                to_artifact_id=dep_file,
                edge_type="imports",
                confidence=1.0,
            )
    search = FakeSearchClient()
    search.seed("retry", [SearchHit(artifact_id=target_syms["GithubRestBackend"], score=0.9)])
    deps = make_broker_deps(factory, search)

    request = ChangeContextRequest(task="Add a retry path to GithubRestBackend in github_rest.py")
    resp = await change_context.create_change_pack(deps, request, _REQUESTER)

    assert len(resp.dependency_files) == change_context._MAX_DEPENDENCY_FILES == 2


async def test_change_pack_enforces_budget_server_side(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The BROKER caps selected tokens (invariant 3): a tiny budget keeps the essential
    target but drops dependency files and records why in notes."""
    big_body = "x" * 8000  # ~2000 est tokens of symbol spans for the dependency file
    async with factory() as session:
        target_file, target_syms = await insert_code_unit(
            session,
            source_uri=_TARGET_URI,
            symbols={"GithubRestBackend": "class GithubRestBackend: ..."},
        )
        dep_file, _ = await insert_code_unit(
            session,
            source_uri=_DEP_URI,
            symbols={"HttpFetchError": "class HttpFetchError(Exception): ...\n" + big_body},
        )
        await insert_edge(
            session,
            from_artifact_id=target_file,
            to_artifact_id=dep_file,
            edge_type="imports",
            confidence=1.0,
        )
    search = FakeSearchClient()
    search.seed("retry", [SearchHit(artifact_id=target_syms["GithubRestBackend"], score=0.9)])
    deps = make_broker_deps(factory, search)

    request = ChangeContextRequest(
        task="Add a retry path to GithubRestBackend in github_rest.py", budget_tokens=100
    )
    resp = await change_context.create_change_pack(deps, request, _REQUESTER)

    assert resp.target_files  # the essential target is always kept
    assert resp.dependency_files == []  # the 2000-token dependency exceeds the 100 budget
    assert any("budget" in n for n in resp.notes)


async def test_change_pack_finds_the_real_test_via_focused_search(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """When there is no `tests` edge (tests carry no graph link) but the test file IS in the KB,
    a focused search must still locate it by name — beating the naming-convention guess, which
    would propose `test_github_rest.py` and miss the real `test_github_rest_backend.py`."""
    async with factory() as session:
        target_sym = await insert_artifact(
            session,
            title="GithubRestBackend",
            body_text="class GithubRestBackend: ...",
            artifact_type="code_symbol",
            source_type="github_code",
            source_uri=_TARGET_URI,
        )
        # A real test file in the KB, NOT linked by a tests edge and NOT in the task's own hits.
        real_test = await insert_artifact(
            session,
            title="test_fetch_retries",
            body_text="def test_fetch_retries(): GithubRestBackend().fetch()",
            artifact_type="test",
            source_type="github_code",
            source_uri=_TEST_URI,
        )
    search = FakeSearchClient()
    search.seed("retry", [SearchHit(artifact_id=target_sym, score=0.9)])
    # The focused test search ("test … github_rest") is the ONLY way this test surfaces.
    search.seed("test", [SearchHit(artifact_id=real_test, score=0.9)])
    deps = make_broker_deps(factory, search)

    request = ChangeContextRequest(task="Add a retry path to GithubRestBackend in github_rest.py")
    resp = await change_context.create_change_pack(deps, request, _REQUESTER)

    assert [f.path for f in resp.test_files] == [_TEST_PATH]
    assert "KB search located" in resp.test_files[0].reason
    assert not any("naming convention" in n for n in resp.notes)


async def test_change_pack_falls_back_to_naming_convention_when_no_test_edge(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """With tests excluded from the KB (no `tests` edge), the selector still PROPOSES a
    conventional test path and flags it for the runtime to verify — it never returns empty."""
    async with factory() as session:
        target_sym = await insert_artifact(
            session,
            title="GithubRestBackend",
            body_text="class GithubRestBackend: ...",
            artifact_type="code_symbol",
            source_type="github_code",
            source_uri=_TARGET_URI,
        )
    search = FakeSearchClient()
    search.seed("retry", [SearchHit(artifact_id=target_sym, score=0.9)])
    deps = make_broker_deps(factory, search)

    request = ChangeContextRequest(task="Add a retry path to GithubRestBackend")
    resp = await change_context.create_change_pack(deps, request, _REQUESTER)

    assert resp.test_files, "must still propose a test file when the graph has no tests edge"
    proposed = resp.test_files[0]
    assert proposed.path.rsplit("/", 1)[-1] == "test_github_rest.py"
    assert "naming convention" in proposed.reason
    assert any("naming convention" in n for n in resp.notes)
