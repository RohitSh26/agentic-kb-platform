"""AdoWikiBackend against a fake httpx transport — no real network.

Asserts: ADO Basic auth header present and correct; the wiki is pinned to its
backing-repo head SHA and that single SHA is the `source_version` on every page,
stable across runs (determinism); SourceRefs carry the right source_type /
source_uri / path; listing is sorted; `fetch_text` returns the page markdown; and
the PAT never leaks into any emitted field or log line. Mirrors
test_github_rest_backend.py's structure.
"""

import base64

import httpx
import pytest

from agentic_kb_builder.connectors.ado_wiki_backend import AdoWikiBackend
from agentic_kb_builder.connectors.http_client import HttpFetchError
from agentic_kb_builder.domain.source_config import AzureWikiSourceSpec

_TOKEN = "ADO_FAKE_PAT_DO_NOT_LOG"
_REPO_ID = "11111111-2222-3333-4444-555555555555"
_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_EXPECTED_AUTH = "Basic " + base64.b64encode(f":{_TOKEN}".encode()).decode("ascii")

# A page subtree: a root with two content children, one of which has its own child.
# Deliberately UNSORTED so the test proves the backend imposes a stable order.
_PAGES = {
    "/Home": "# Home\nwelcome\n",
    "/Guides/Setup": "# Setup\nrun make sync\n",
    "/Guides": "# Guides\nindex of guides\n",
}

_TREE = {
    "id": 1,
    "path": "/",
    "subPages": [
        {
            "id": 3,
            "path": "/Guides",
            "isParentPage": True,
            "subPages": [
                {"id": 4, "path": "/Guides/Setup", "subPages": []},
            ],
        },
        {"id": 2, "path": "/Home", "subPages": []},
    ],
}


def _make_handler() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        # PAT must always be present and correct, but never in path/query.
        assert request.headers.get("Authorization") == _EXPECTED_AUTH
        path = request.url.path
        assert _TOKEN not in str(request.url)

        if path == "/myorg/proj/_apis/wiki/wikis/mywiki":
            return httpx.Response(200, json={"id": "wiki-id", "repositoryId": _REPO_ID})
        if path == f"/myorg/proj/_apis/git/repositories/{_REPO_ID}":
            return httpx.Response(200, json={"defaultBranch": "refs/heads/wikiMaster"})
        if path == f"/myorg/proj/_apis/git/repositories/{_REPO_ID}/refs":
            assert request.url.params.get("filter") == "heads/wikiMaster"
            return httpx.Response(
                200, json={"value": [{"name": "refs/heads/wikiMaster", "objectId": _SHA}]}
            )
        if path == "/myorg/proj/_apis/wiki/wikis/mywiki/pages":
            page_path = request.url.params.get("path")
            if request.url.params.get("recursionLevel") == "full":
                return httpx.Response(200, json=_TREE)
            # Single-page content fetch.
            assert request.url.params.get("includeContent") == "true"
            assert page_path in _PAGES
            return httpx.Response(200, json={"path": page_path, "content": _PAGES[page_path]})
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


def _spec() -> AzureWikiSourceSpec:
    return AzureWikiSourceSpec(
        type="azure_wiki", name="w", organization="myorg", project="proj", wiki="mywiki"
    )


async def test_list_sources_pins_head_sha_and_emits_refs() -> None:
    backend = AdoWikiBackend(_spec(), _TOKEN, client_transport=_make_handler())
    refs = await backend.list_sources()

    # ADO returns "/Home"; the backend strips the leading slash so globs can match.
    assert {r.path for r in refs} == {p.removeprefix("/") for p in _PAGES}
    for ref in refs:
        assert ref.source_version == _SHA
        assert ref.source_type == "azure_wiki"
        # source_uri keeps the slash separator even though ref.path is slash-relative
        assert ref.source_uri == f"azuredevops://myorg/proj/_wiki/wikis/mywiki/{ref.path}"
        # token never embedded in identity fields
        assert _TOKEN not in ref.source_uri
        assert _TOKEN not in ref.source_version


async def test_list_sources_is_sorted_by_path() -> None:
    # The mock returns the subtree unsorted; the backend must impose a stable order.
    backend = AdoWikiBackend(_spec(), _TOKEN, client_transport=_make_handler())
    paths = [r.path for r in await backend.list_sources()]
    assert paths == sorted(p.removeprefix("/") for p in _PAGES)


async def test_source_version_is_stable_across_runs() -> None:
    backend = AdoWikiBackend(_spec(), _TOKEN, client_transport=_make_handler())
    first = {r.source_uri: r.source_version for r in await backend.list_sources()}
    second = {r.source_uri: r.source_version for r in await backend.list_sources()}
    assert first == second
    assert set(first.values()) == {_SHA}


async def test_fetch_text_returns_page_markdown() -> None:
    backend = AdoWikiBackend(_spec(), _TOKEN, client_transport=_make_handler())
    refs = await backend.list_sources()
    by_path = {r.path: r for r in refs}
    # ref.path is slash-relative; fetch_text must re-add the slash for the ADO API
    # (the handler asserts the requested path is one of _PAGES, which are slash-prefixed).
    text = await backend.fetch_text(by_path["Guides/Setup"])
    assert text == _PAGES["/Guides/Setup"]


async def test_wiki_paths_are_matchable_by_include_globs() -> None:
    """Regression: ADO wiki paths used to keep their leading slash, so NO include glob
    (not even '**') could match them and every wiki page was silently dropped."""
    from agentic_kb_builder.domain.source_config import PathFilter

    backend = AdoWikiBackend(_spec(), _TOKEN, client_transport=_make_handler())
    refs = await backend.list_sources()
    assert any(PathFilter(["Guides/**"]).matches(r.path or "") for r in refs)
    assert all(PathFilter(["**"]).matches(r.path or "") for r in refs)


async def test_basic_auth_header_is_correct() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        path = request.url.path
        if path == "/myorg/proj/_apis/wiki/wikis/mywiki":
            return httpx.Response(200, json={"repositoryId": _REPO_ID})
        if path == f"/myorg/proj/_apis/git/repositories/{_REPO_ID}":
            return httpx.Response(200, json={"defaultBranch": "refs/heads/main"})
        if path == f"/myorg/proj/_apis/git/repositories/{_REPO_ID}/refs":
            return httpx.Response(200, json={"value": [{"objectId": _SHA}]})
        return httpx.Response(200, json=_TREE)

    backend = AdoWikiBackend(_spec(), _TOKEN, client_transport=httpx.MockTransport(handler))
    await backend.list_sources()
    assert seen and all(h == _EXPECTED_AUTH for h in seen)
    # Sanity: the header decodes to ":<token>" (empty user, PAT as password).
    raw = base64.b64decode(_EXPECTED_AUTH.removeprefix("Basic ")).decode("ascii")
    assert raw == f":{_TOKEN}"


async def test_no_token_in_any_field_or_log(caplog: pytest.LogCaptureFixture) -> None:
    backend = AdoWikiBackend(_spec(), _TOKEN, client_transport=_make_handler())
    with caplog.at_level("INFO"):
        refs = await backend.list_sources()
        await backend.fetch_text(refs[0])
    assert _TOKEN not in caplog.text
    for ref in refs:
        assert _TOKEN not in ref.model_dump_json()


async def test_missing_repository_id_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/myorg/proj/_apis/wiki/wikis/mywiki":
            return httpx.Response(200, json={"id": "wiki-id"})  # no repositoryId
        return httpx.Response(404)

    backend = AdoWikiBackend(_spec(), _TOKEN, client_transport=httpx.MockTransport(handler))
    with pytest.raises(HttpFetchError, match="repositoryId"):
        await backend.list_sources()


async def test_page_without_content_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/myorg/proj/_apis/wiki/wikis/mywiki":
            return httpx.Response(200, json={"repositoryId": _REPO_ID})
        if path == f"/myorg/proj/_apis/git/repositories/{_REPO_ID}":
            return httpx.Response(200, json={"defaultBranch": "refs/heads/main"})
        if path == f"/myorg/proj/_apis/git/repositories/{_REPO_ID}/refs":
            return httpx.Response(200, json={"value": [{"objectId": _SHA}]})
        if path == "/myorg/proj/_apis/wiki/wikis/mywiki/pages":
            if request.url.params.get("recursionLevel") == "full":
                return httpx.Response(200, json=_TREE)
            return httpx.Response(200, json={"path": "/Home"})  # no content field
        return httpx.Response(404)

    backend = AdoWikiBackend(_spec(), _TOKEN, client_transport=httpx.MockTransport(handler))
    refs = await backend.list_sources()
    with pytest.raises(HttpFetchError, match="no content"):
        await backend.fetch_text(refs[0])
