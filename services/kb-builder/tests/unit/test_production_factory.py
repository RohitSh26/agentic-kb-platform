"""production_backend_factory dispatch: each type -> the right backend class;
ADO stubs raise NotImplementedError; unsupported type raises SourceConfigError."""

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


def test_dispatches_ado_stubs() -> None:
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


async def test_ado_wiki_stub_raises_not_implemented() -> None:
    backend = AdoWikiBackend(
        AzureWikiSourceSpec(type="azure_wiki", name="w", organization="o", project="p", wiki="k"),
        None,
    )
    with pytest.raises(NotImplementedError, match="ADO Wiki PR"):
        await backend.list_sources()


async def test_ado_work_item_stub_raises_not_implemented() -> None:
    backend = AdoWorkItemBackend(
        AdoCardSourceSpec(type="ado_card", name="a", organization="o", project="p"), None
    )
    with pytest.raises(NotImplementedError, match="ADO Work Items PR"):
        await backend.list_sources()


def test_unsupported_type_raises() -> None:
    factory = production_backend_factory()

    class _FakeSpec:
        type = "git_metadata"

    with pytest.raises(SourceConfigError, match="unsupported source type"):
        factory(_FakeSpec(), None)  # type: ignore[arg-type]
