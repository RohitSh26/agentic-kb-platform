"""GitHubRestBackend against a fake httpx transport — no real network.

Asserts: branch -> SHA resolution, source_version == that SHA and stable across
runs (determinism), file list matches the tree, fetch_text base64-decodes, and no
token leaks into any emitted SourceRef field or log line.
"""

import base64

import httpx
import pytest

from agentic_kb_builder.connectors.github_rest import GitHubRestBackend
from agentic_kb_builder.connectors.http_client import HttpFetchError
from agentic_kb_builder.domain.source_config import GithubCodeSourceSpec, GithubDocSourceSpec

_SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
_TOKEN = "ghp_FAKE_SECRET_DO_NOT_LOG"

_FILES = {
    "src/app.py": "x = 1\n",
    "README.md": "# Title\nbody\n",
    "docs/guide.md": "guide\n",
}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _make_handler(*, truncated: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        # Token must always be present and correct, but never in path/query.
        assert request.headers.get("Authorization") == f"Bearer {_TOKEN}"
        path = request.url.path
        assert _TOKEN not in str(request.url)
        if path == "/repos/octo/widget/branches/main":
            return httpx.Response(200, json={"commit": {"sha": _SHA}})
        if path == f"/repos/octo/widget/git/trees/{_SHA}":
            assert request.url.params.get("recursive") == "1"
            tree = [{"path": p, "type": "blob", "sha": "x"} for p in sorted(_FILES)]
            tree.append({"path": "src", "type": "tree", "sha": "y"})
            return httpx.Response(200, json={"sha": _SHA, "tree": tree, "truncated": truncated})
        if path.startswith("/repos/octo/widget/contents/"):
            assert request.url.params.get("ref") == _SHA
            file_path = path.removeprefix("/repos/octo/widget/contents/")
            return httpx.Response(
                200, json={"encoding": "base64", "content": _b64(_FILES[file_path])}
            )
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


def _code_spec() -> GithubCodeSourceSpec:
    return GithubCodeSourceSpec(type="github_code", name="widget", repo="octo/widget")


async def test_list_sources_resolves_sha_and_emits_refs() -> None:
    backend = GitHubRestBackend(_code_spec(), _TOKEN, client_transport=_make_handler())
    refs = await backend.list_sources()

    assert {r.path for r in refs} == set(_FILES)  # blobs only, tree entry dropped
    for ref in refs:
        assert ref.source_version == _SHA
        assert ref.source_type == "github_code"
        assert ref.source_uri == f"github://octo/widget/{ref.path}"
        assert ref.repo == "octo/widget"
        assert ref.branch == "main"
        # token never embedded in identity fields
        assert _TOKEN not in ref.source_uri
        assert _TOKEN not in ref.source_version


async def test_source_version_is_stable_across_runs() -> None:
    backend = GitHubRestBackend(_code_spec(), _TOKEN, client_transport=_make_handler())
    first = {r.source_uri: r.source_version for r in await backend.list_sources()}
    second = {r.source_uri: r.source_version for r in await backend.list_sources()}
    assert first == second
    assert set(first.values()) == {_SHA}


async def test_fetch_text_base64_decodes() -> None:
    backend = GitHubRestBackend(_code_spec(), _TOKEN, client_transport=_make_handler())
    refs = await backend.list_sources()
    by_path = {r.path: r for r in refs}
    text = await backend.fetch_text(by_path["src/app.py"])
    assert text == _FILES["src/app.py"]


async def test_doc_spec_emits_github_doc_type() -> None:
    spec = GithubDocSourceSpec(type="github_doc", name="widget-docs", repo="octo/widget")
    backend = GitHubRestBackend(spec, _TOKEN, client_transport=_make_handler())
    refs = await backend.list_sources()
    assert all(r.source_type == "github_doc" for r in refs)


async def test_truncated_tree_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    backend = GitHubRestBackend(
        _code_spec(), _TOKEN, client_transport=_make_handler(truncated=True)
    )
    with caplog.at_level("WARNING"):
        refs = await backend.list_sources()
    assert refs  # partial listing still returned
    assert "github_tree_truncated" in caplog.text


async def test_unexpected_encoding_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/branches/main":
            return httpx.Response(200, json={"commit": {"sha": _SHA}})
        if request.url.path == f"/repos/octo/widget/git/trees/{_SHA}":
            return httpx.Response(
                200,
                json={"sha": _SHA, "tree": [{"path": "a.py", "type": "blob"}], "truncated": False},
            )
        return httpx.Response(200, json={"encoding": "utf-8", "content": "raw"})

    backend = GitHubRestBackend(_code_spec(), _TOKEN, client_transport=httpx.MockTransport(handler))
    refs = await backend.list_sources()
    with pytest.raises(HttpFetchError):
        await backend.fetch_text(refs[0])


async def test_no_token_in_any_log(caplog: pytest.LogCaptureFixture) -> None:
    backend = GitHubRestBackend(_code_spec(), _TOKEN, client_transport=_make_handler())
    with caplog.at_level("INFO"):
        refs = await backend.list_sources()
        await backend.fetch_text(refs[0])
    assert _TOKEN not in caplog.text
