"""GitHub REST FetchBackend (ADR-0015): determinism via a pinned commit SHA.

One backend serves both `github_code` and `github_doc` — `spec.type` decides the
emitted `source_type`; include/exclude globs are applied upstream by
`FilteredFetchBackend`, so this backend only enumerates and reads.

Flow (all at a single resolved SHA so the build is reproducible):
  1. resolve branch -> commit SHA   GET /repos/{owner}/{repo}/branches/{branch}
  2. list blob paths                GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1
  3. read one file                  GET /repos/{owner}/{repo}/contents/{path}?ref={sha}

`source_version` is the commit SHA. Same repo state => same SHA => same content_hash.
The auth token is injected as `Authorization: Bearer <pat>` by the HTTP client and
never appears in a SourceRef field or a log line.
"""

import base64
from typing import Any, Literal

from agentic_kb_builder.connectors.http_client import AsyncHttpClient, HttpFetchError
from agentic_kb_builder.domain.source_config import GithubCodeSourceSpec, GithubDocSourceSpec
from agentic_kb_builder.domain.source_records import SourceRef
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_API_BASE = "https://api.github.com"
GithubSourceSpec = GithubCodeSourceSpec | GithubDocSourceSpec


def _github_client(token: str | None, transport: Any | None = None) -> AsyncHttpClient:
    return AsyncHttpClient(
        base_url=_API_BASE,
        auth_header=f"Bearer {token}" if token else None,
        extra_headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        transport=transport,
    )


class GitHubRestBackend:
    """FetchBackend backed by the GitHub REST API, pinned to a commit SHA.

    `client_transport` (e.g. `httpx.MockTransport`) makes the backend hermetic in
    tests; production passes none and a real pool is used.
    """

    def __init__(
        self,
        spec: GithubSourceSpec,
        token: str | None,
        *,
        client_transport: Any | None = None,
    ) -> None:
        self._spec = spec
        self._token = token
        self._transport = client_transport
        self._source_type: Literal["github_code", "github_doc"] = spec.type
        owner, _, name = spec.repo.partition("/")
        self._owner = owner
        self._repo = name
        self._branch = spec.branch

    def _new_client(self) -> AsyncHttpClient:
        return _github_client(self._token, self._transport)

    async def _resolve_sha(self, client: AsyncHttpClient) -> str:
        data = await client.get_json(f"/repos/{self._owner}/{self._repo}/branches/{self._branch}")
        sha = data["commit"]["sha"]
        if not isinstance(sha, str) or not sha:
            raise HttpFetchError(
                f"github branch {self._branch!r} returned no commit sha for "
                f"{self._owner}/{self._repo}"
            )
        logger.info(
            "event=github_branch_resolved repo=%s/%s branch=%s sha=%s",
            self._owner,
            self._repo,
            self._branch,
            sha,
        )
        return sha

    async def list_sources(self) -> list[SourceRef]:
        async with self._new_client() as client:
            sha = await self._resolve_sha(client)
            tree = await client.get_json(
                f"/repos/{self._owner}/{self._repo}/git/trees/{sha}",
                params={"recursive": "1"},
            )
        if tree.get("truncated"):
            # Known limitation (ADR-0015): very large trees are capped by the API.
            logger.warning(
                "event=github_tree_truncated repo=%s/%s sha=%s "
                "msg=partial-listing-large-repo-see-adr-0015",
                self._owner,
                self._repo,
                sha,
            )
        refs: list[SourceRef] = []
        for entry in tree.get("tree", []):
            if entry.get("type") != "blob":
                continue
            path = entry["path"]
            refs.append(
                SourceRef(
                    source_type=self._source_type,
                    source_uri=f"github://{self._owner}/{self._repo}/{path}",
                    source_version=sha,
                    repo=self._spec.repo,
                    branch=self._branch,
                    path=path,
                )
            )
        logger.info(
            "event=github_listed repo=%s/%s sha=%s blobs=%d",
            self._owner,
            self._repo,
            sha,
            len(refs),
        )
        return refs

    async def fetch_text(self, source: SourceRef) -> str:
        path = source.path or ""
        async with self._new_client() as client:
            data = await client.get_json(
                f"/repos/{self._owner}/{self._repo}/contents/{path}",
                params={"ref": source.source_version},
            )
        encoding = data.get("encoding")
        if encoding != "base64":
            raise HttpFetchError(
                f"github contents for {path!r} used unexpected encoding {encoding!r}"
            )
        # The contents API wraps base64 with newlines; decode tolerates them.
        raw = base64.b64decode(data["content"])
        return raw.decode("utf-8")


__all__ = ["GitHubRestBackend"]
