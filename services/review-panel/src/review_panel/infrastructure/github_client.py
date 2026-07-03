"""Read-only GitHub REST adapter behind the GitHubClient port.

Dev gate by construction (ADR-0031): the adapter's only capability is fetching
PR metadata + diff over GET. There is no method that writes to GitHub — no
review, comment, approval, or request-changes can originate here, and the
service holds no GitHub write credential (the token is optional and read-only;
unauthenticated works for public repos). The token never appears in logs.
"""

from typing import Any, Protocol

import httpx

from review_panel.domain.errors import GitHubAPIError
from review_panel.domain.pr import PRContext
from review_panel.structured_logging import get_logger

logger = get_logger("review_panel.infrastructure.github_client")

_TIMEOUT_SECONDS = 60.0


class GitHubClient(Protocol):
    """The single PR capability the draft engine needs — nothing else is reachable."""

    async def get_pr(self, repo: str, number: int) -> PRContext: ...


class HttpxGitHubClient:
    def __init__(
        self,
        token: str = "",
        api_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._transport = transport

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(
        self, url: str, *, accept: str = "application/vnd.github+json"
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = await client.get(url, headers=self._headers(accept))
        except httpx.HTTPError as exc:
            raise GitHubAPIError(f"github request failed: {type(exc).__name__}: {exc}") from exc
        logger.info("event=github_call method=GET url=%s status=%s", url, response.status_code)
        if response.status_code >= 400:
            raise GitHubAPIError(f"github returned {response.status_code} for GET {url}")
        return response

    async def get_pr(self, repo: str, number: int) -> PRContext:
        base = f"{self._api_url}/repos/{repo}/pulls/{number}"
        meta: dict[str, Any] = (await self._get(base)).json()
        diff = (await self._get(base, accept="application/vnd.github.diff")).text
        user: dict[str, Any] = meta.get("user") or {}
        return PRContext(
            repo=repo,
            number=number,
            head_sha=str(meta["head"]["sha"]),
            title=str(meta.get("title") or ""),
            body=str(meta.get("body") or ""),
            author=str(user.get("login") or ""),
            diff=diff,
        )
