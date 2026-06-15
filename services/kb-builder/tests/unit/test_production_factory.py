"""production_backend_factory dispatch: each source type -> the right backend
class; an unsupported type raises SourceConfigError. The ADO backends are now
implemented (their fetch behavior is covered by test_ado_*_backend.py)."""

import httpx
import pytest

from agentic_kb_builder.connectors.ado_wiki_backend import AdoWikiBackend
from agentic_kb_builder.connectors.ado_work_item_backend import AdoWorkItemBackend
from agentic_kb_builder.connectors.config_loader import SourceConfigError
from agentic_kb_builder.connectors.github_rest import GitHubRestBackend
from agentic_kb_builder.connectors.production_factory import production_backend_factory
from agentic_kb_builder.domain.source_config import (
    AdoCardSourceSpec,
    AzureWikiSourceSpec,
    GithubCodeSourceSpec,
    GithubDocSourceSpec,
)


def test_dispatches_github_code_and_doc() -> None:
    factory = production_backend_factory()
    code = factory(GithubCodeSourceSpec(type="github_code", name="c", repo="o/r"), "tok")
    doc = factory(GithubDocSourceSpec(type="github_doc", name="d", repo="o/r"), "tok")
    assert isinstance(code, GitHubRestBackend)
    assert isinstance(doc, GitHubRestBackend)


def test_dispatches_ado_backends() -> None:
    factory = production_backend_factory()
    wiki = factory(
        AzureWikiSourceSpec(type="azure_wiki", name="w", organization="o", project="p", wiki="k"),
        "tok",
    )
    card = factory(
        AdoCardSourceSpec(type="ado_card", name="a", organization="o", project="p"), "tok"
    )
    assert isinstance(wiki, AdoWikiBackend)
    assert isinstance(card, AdoWorkItemBackend)


async def test_ado_wiki_backend_lists_pages_via_factory() -> None:
    # azure_wiki dispatches to a working AdoWikiBackend that pins the wiki's
    # backing git head SHA as the shared source_version.
    repo_id = "repo-1"
    sha = "feedface" * 5

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/o/p/_apis/wiki/wikis/k":
            return httpx.Response(200, json={"repositoryId": repo_id})
        if path == f"/o/p/_apis/git/repositories/{repo_id}":
            return httpx.Response(200, json={"defaultBranch": "refs/heads/main"})
        if path == f"/o/p/_apis/git/repositories/{repo_id}/refs":
            return httpx.Response(200, json={"value": [{"objectId": sha}]})
        if path == "/o/p/_apis/wiki/wikis/k/pages":
            return httpx.Response(200, json={"path": "/", "subPages": [{"id": 1, "path": "/Home"}]})
        return httpx.Response(404)

    factory = production_backend_factory(client_transport=httpx.MockTransport(handler))
    backend = factory(
        AzureWikiSourceSpec(type="azure_wiki", name="w", organization="o", project="p", wiki="k"),
        "tok",
    )
    assert isinstance(backend, AdoWikiBackend)
    refs = await backend.list_sources()
    assert [r.path for r in refs] == ["/Home"]
    assert all(r.source_version == sha for r in refs)


def test_dispatches_ado_work_item_backend() -> None:
    # ado_card dispatches to the implemented AdoWorkItemBackend (no longer a stub);
    # its fetch behavior is covered by test_ado_work_item_backend.py.
    factory = production_backend_factory()
    card = factory(
        AdoCardSourceSpec(type="ado_card", name="a", organization="o", project="p"), "tok"
    )
    assert isinstance(card, AdoWorkItemBackend)


def test_unsupported_type_raises() -> None:
    factory = production_backend_factory()

    class _FakeSpec:
        type = "git_metadata"

    with pytest.raises(SourceConfigError, match="unsupported source type"):
        factory(_FakeSpec(), None)  # type: ignore[arg-type]
