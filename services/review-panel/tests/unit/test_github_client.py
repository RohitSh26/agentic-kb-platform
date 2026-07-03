"""GitHub adapter: read-only by construction (GET only, token optional)."""

from collections.abc import Callable

import httpx
import pytest

from review_panel.domain.errors import GitHubAPIError
from review_panel.infrastructure.github_client import HttpxGitHubClient


def _client(
    handler: Callable[[httpx.Request], httpx.Response], token: str = "token"
) -> HttpxGitHubClient:
    return HttpxGitHubClient(token, "https://gh.test", transport=httpx.MockTransport(handler))


async def test_get_pr_combines_metadata_and_diff() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.headers["Accept"] == "application/vnd.github.diff":
            return httpx.Response(200, text="diff --git a/x b/x")
        return httpx.Response(
            200,
            json={
                "head": {"sha": "abc"},
                "title": "T",
                "body": None,
                "user": {"login": "dev"},
            },
        )

    pr = await _client(handler).get_pr("acme/platform", 7)
    assert pr.head_sha == "abc"
    assert pr.body == ""  # null body normalized
    assert pr.diff.startswith("diff --git")


async def test_api_failure_raises_github_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(GitHubAPIError):
        await _client(handler).get_pr("acme/platform", 7)


async def test_token_only_in_header_never_in_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["Accept"] == "application/vnd.github.diff":
            return httpx.Response(200, text="")
        return httpx.Response(200, json={"head": {"sha": "abc"}})

    await _client(handler).get_pr("acme/platform", 7)
    assert seen[0].headers["Authorization"] == "Bearer token"
    assert "token" not in str(seen[0].url)


async def test_without_token_requests_are_unauthenticated() -> None:
    """The service needs no GitHub credential at all for public repos —
    it certainly holds no write credential (ADR-0031)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["Accept"] == "application/vnd.github.diff":
            return httpx.Response(200, text="")
        return httpx.Response(200, json={"head": {"sha": "abc"}})

    await _client(handler, token="").get_pr("acme/platform", 7)
    assert all("Authorization" not in request.headers for request in seen)
