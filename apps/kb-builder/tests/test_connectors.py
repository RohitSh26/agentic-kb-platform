import pytest

from common.hashing import content_hash
from contracts.artifact_schemas import SourceRef, SourceType
from kb_builder.connectors import (
    AdoCardConnector,
    AzureWikiConnector,
    BaseConnector,
    GitHubCodeConnector,
    GitHubDocConnector,
)


class FakeBackend:
    def __init__(self, sources: list[SourceRef], texts: dict[str, str]) -> None:
        self._sources = sources
        self._texts = texts

    async def list_sources(self) -> list[SourceRef]:
        return self._sources

    async def fetch_text(self, source: SourceRef) -> str:
        return self._texts[source.source_uri]


def _ref(source_type: SourceType, uri: str, version: str) -> SourceRef:
    return SourceRef(source_type=source_type, source_uri=uri, source_version=version)


CASES: list[tuple[type[BaseConnector], SourceType, str, str]] = [
    (GitHubCodeConnector, "github_code", "https://github.com/o/r/blob/sha/a.py", "0123abc"),
    (GitHubDocConnector, "github_doc", "https://github.com/o/r/blob/sha/README.md", "0123abc"),
    (AzureWikiConnector, "azure_wiki", "https://dev.azure.com/o/wiki/page", "7"),
    (AdoCardConnector, "ado_card", "https://dev.azure.com/o/workitems/42", "3"),
]


@pytest.mark.parametrize(("connector_cls", "source_type", "uri", "version"), CASES)
async def test_connector_returns_identity_and_hash(
    connector_cls: type[BaseConnector],
    source_type: SourceType,
    uri: str,
    version: str,
) -> None:
    ref = _ref(source_type, uri, version)
    backend = FakeBackend([ref], {uri: "Title  \r\nBody\r\n"})
    connector = connector_cls(backend)

    sources = await connector.list_sources()
    assert sources == [ref]

    result = await connector.fetch(ref)
    assert result.source.source_type == source_type
    assert result.source.source_uri == uri
    assert result.source.source_version == version
    assert result.content_hash == content_hash(result.text)


@pytest.mark.parametrize(("connector_cls", "source_type", "uri", "version"), CASES)
async def test_fetch_is_deterministic_across_instances(
    connector_cls: type[BaseConnector],
    source_type: SourceType,
    uri: str,
    version: str,
) -> None:
    ref = _ref(source_type, uri, version)
    raw = "Line one  \r\nLine two\r\n"
    first = await connector_cls(FakeBackend([ref], {uri: raw})).fetch(ref)
    second = await connector_cls(FakeBackend([ref], {uri: raw})).fetch(ref)
    assert first.content_hash == second.content_hash
    assert first.text == second.text


async def test_github_code_normalization_is_conservative() -> None:
    ref = _ref("github_code", "https://github.com/o/r/blob/sha/a.py", "0123abc")
    raw = "x = 1  \r\ny = 2\n"
    result = await GitHubCodeConnector(FakeBackend([ref], {ref.source_uri: raw})).fetch(ref)
    # line endings unified, but trailing whitespace preserved (exact code evidence)
    assert result.text == "x = 1  \ny = 2\n"


async def test_doc_normalization_strips_trailing_whitespace() -> None:
    ref = _ref("github_doc", "https://github.com/o/r/blob/sha/README.md", "0123abc")
    raw = "Heading  \r\n\r\nBody\r\n\r\n"
    result = await GitHubDocConnector(FakeBackend([ref], {ref.source_uri: raw})).fetch(ref)
    assert result.text == "Heading\n\nBody\n"


async def test_list_sources_rejects_wrong_source_type() -> None:
    wrong = _ref("ado_card", "https://dev.azure.com/o/workitems/42", "3")
    connector = GitHubCodeConnector(FakeBackend([wrong], {}))
    with pytest.raises(ValueError, match="source_type"):
        await connector.list_sources()
