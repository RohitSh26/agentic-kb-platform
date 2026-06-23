"""GitHub REST FetchBackend: determinism via a pinned commit SHA.

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

import re
from typing import Any, Literal

from agentic_kb_builder.connectors.http_client import AsyncHttpClient, HttpFetchError
from agentic_kb_builder.domain.source_config import GithubCodeSourceSpec, GithubDocSourceSpec
from agentic_kb_builder.domain.source_records import SourceRef
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_API_BASE = "https://api.github.com"
_JSON_ACCEPT = "application/vnd.github+json"
# Raw media type returns the file bytes directly — unlike the JSON+base64 form it is
# not capped at 1 MB, so files >1 MB are fetched whole (not silently truncated).
_RAW_ACCEPT = "application/vnd.github.raw"
GithubSourceSpec = GithubCodeSourceSpec | GithubDocSourceSpec

# A git branch may contain internal "/" (feature/x) but never whitespace, "..",
# or a leading/trailing slash. Guards a config-supplied branch interpolated into
# the request path (host is already pinned to api.github.com).
_BRANCH_RE = re.compile(r"^[^\s/](?:[^\s]*[^\s/])?$")


def _validate_branch(branch: str) -> str:
    if ".." in branch or _BRANCH_RE.match(branch) is None:
        raise HttpFetchError(f"invalid github branch name {branch!r}")
    return branch


def _github_client(
    token: str | None, *, accept: str = _JSON_ACCEPT, transport: Any | None = None
) -> AsyncHttpClient:
    return AsyncHttpClient(
        base_url=_API_BASE,
        auth_header=f"Bearer {token}" if token else None,
        extra_headers={
            "Accept": accept,
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
        self._branch = _validate_branch(spec.branch)
        # A github source with no token runs unauthenticated and will 404 on a private
        # repo (GitHub hides private repos behind 404). Surface the misconfig at build
        # start, not 100 fetches later — auth.token_env is optional only for PUBLIC repos.
        if token is None:
            logger.warning(
                "event=github_source_unauthenticated repo=%s/%s type=%s "
                "msg=no-auth.token_env-configured-private-repos-return-404",
                owner,
                name,
                spec.type,
            )

    def _new_client(self, *, accept: str = _JSON_ACCEPT) -> AsyncHttpClient:
        return _github_client(self._token, accept=accept, transport=self._transport)

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
            # Known limitation: very large trees are capped by the API.
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
        # Stable ordering regardless of the API's tree order (connectors rule).
        refs.sort(key=lambda ref: ref.path or "")
        logger.info(
            "event=github_listed repo=%s/%s sha=%s blobs=%d",
            self._owner,
            self._repo,
            sha,
            len(refs),
        )
        return refs

    async def fetch_text(self, source: SourceRef) -> str:
        # Raw media type returns the file bytes directly at the pinned SHA. This
        # avoids the contents API's JSON+base64 1 MB cap, which would otherwise
        # return empty content for a >1 MB file and hash it as an empty file.
        path = source.path or ""
        async with self._new_client(accept=_RAW_ACCEPT) as client:
            return await client.get_text(
                f"/repos/{self._owner}/{self._repo}/contents/{path}",
                params={"ref": source.source_version},
            )


__all__ = ["GitHubRestBackend"]
