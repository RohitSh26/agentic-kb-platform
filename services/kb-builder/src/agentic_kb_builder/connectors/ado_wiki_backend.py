"""Azure DevOps Wiki FetchBackend (ADR-0015): determinism via the wiki's git head.

An ADO wiki is backed by a git repository, so — mirroring how `GitHubRestBackend`
pins one commit SHA before listing/reading — this backend resolves the wiki's
backing-repo head commit ONCE and stamps every emitted page with that single
`source_version`. Same wiki state => same head SHA => same `content_hash`
(connectors rule), and a re-run against an unchanged wiki is byte-identical.

Flow (all over the ADO REST API, HTTP Basic auth `Authorization: Basic base64(":"+PAT)`):
  1. resolve the wiki        GET /{project}/_apis/wiki/wikis/{wiki}
                             -> repositoryId (the backing git repo)
  2. resolve the head SHA    GET /{project}/_apis/git/repositories/{repoId}
                             -> defaultBranch, then
                             GET .../refs?filter=heads/{branch} -> objectId
  3. list page subtree       GET /{project}/_apis/wiki/wikis/{wiki}/pages
                                 ?path=/&recursionLevel=full
                             -> walk subPages, emit one SourceRef per content page
  4. read one page           GET /{project}/_apis/wiki/wikis/{wiki}/pages
                                 ?path={page.path}&includeContent=true
                             -> raw markdown (UTF-8 strict)

`source_version` is the head commit SHA shared by all pages. The PAT is injected as
`Authorization: Basic <base64>` by the HTTP client and NEVER appears in a SourceRef
field, a source_uri/source_version/content_hash, or a log line. Retrieved page text is
untrusted content and never influences control flow.
"""

import base64
from typing import Any

from agentic_kb_builder.connectors.http_client import AsyncHttpClient, HttpFetchError
from agentic_kb_builder.domain.source_config import AzureWikiSourceSpec
from agentic_kb_builder.domain.source_records import SourceRef
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_API_VERSION = "7.1"


def _basic_auth_header(token: str | None) -> str | None:
    """ADO PAT auth: HTTP Basic with empty username and the PAT as the password.

    Returns ``Basic base64(":" + token)`` — the raw token is encoded here and only
    here; it is never logged and never stored on a SourceRef.
    """
    if not token:
        return None
    encoded = base64.b64encode(f":{token}".encode()).decode("ascii")
    return f"Basic {encoded}"


class AdoWikiBackend:
    """FetchBackend backed by the Azure DevOps Wiki REST API, pinned to the wiki's git head.

    `client_transport` (e.g. `httpx.MockTransport`) makes the backend hermetic in
    tests; production passes none and a real pool is used.
    """

    def __init__(
        self,
        spec: AzureWikiSourceSpec,
        token: str | None,
        *,
        client_transport: Any | None = None,
    ) -> None:
        self._spec = spec
        self._token = token
        self._transport = client_transport
        self._org = spec.organization
        self._project = spec.project
        self._wiki = spec.wiki
        # base_url carries no path so a leading-slash request path is not joined
        # under it (httpx would otherwise prepend the org twice); the full
        # /{org}/{project}/... path is built per-request via _path().
        self._base_url = "https://dev.azure.com"

    def _path(self, suffix: str) -> str:
        return f"/{self._org}/{self._project}{suffix}"

    def _new_client(self) -> AsyncHttpClient:
        return AsyncHttpClient(
            base_url=self._base_url,
            auth_header=_basic_auth_header(self._token),
            transport=self._transport,
        )

    async def _resolve_version(self, client: AsyncHttpClient) -> str:
        """Pin the wiki to its backing-repo head commit SHA (shared by all pages).

        Same wiki state => same SHA => same content_hash, exactly like the GitHub
        backend pins one commit before listing.
        """
        wiki = await client.get_json(
            self._path(f"/_apis/wiki/wikis/{self._wiki}"),
            params={"api-version": _API_VERSION},
        )
        repo_id = wiki.get("repositoryId")
        if not isinstance(repo_id, str) or not repo_id:
            raise HttpFetchError(
                f"ado wiki {self._wiki!r} in {self._org}/{self._project} returned no repositoryId"
            )
        repo = await client.get_json(
            self._path(f"/_apis/git/repositories/{repo_id}"),
            params={"api-version": _API_VERSION},
        )
        default_branch = repo.get("defaultBranch") or "refs/heads/main"
        # defaultBranch is "refs/heads/<name>"; the refs filter takes "heads/<name>".
        ref_filter = default_branch.removeprefix("refs/")
        refs = await client.get_json(
            self._path(f"/_apis/git/repositories/{repo_id}/refs"),
            params={"filter": ref_filter, "api-version": _API_VERSION},
        )
        values = refs.get("value", [])
        sha = values[0].get("objectId") if values else None
        if not isinstance(sha, str) or not sha:
            raise HttpFetchError(
                f"ado wiki {self._wiki!r} backing repo {repo_id} returned no head commit"
            )
        logger.info(
            "event=ado_wiki_version_resolved org=%s project=%s wiki=%s sha=%s",
            self._org,
            self._project,
            self._wiki,
            sha,
        )
        return sha

    def _walk(self, page: dict[str, Any], refs: list[SourceRef], sha: str) -> None:
        """Depth-first walk of the page subtree; emit a SourceRef per content page.

        A node with no `path` (or the synthetic root "/") is treated as structure
        only; pure folders (`isParentPage` with no content) still get a ref iff they
        carry a real path — ADO models a folder page and its content as one node, so
        we key on having a non-root path rather than on `isParentPage`.
        """
        path = page.get("path")
        if isinstance(path, str) and path not in ("", "/"):
            # ADO returns wiki paths with a LEADING SLASH ("/Architecture"). Strip it on
            # SourceRef.path so include/exclude globs can match — the glob grammar forbids
            # a leading '/', so an un-stripped path can never match any pattern (not even
            # '**'), which silently dropped every wiki page. source_uri keeps the slash as
            # a separator; fetch_text re-adds it for the ADO API.
            rel_path = path.removeprefix("/")
            refs.append(
                SourceRef(
                    source_type="azure_wiki",
                    source_uri=(
                        f"azuredevops://{self._org}/{self._project}/_wiki/wikis/{self._wiki}{path}"
                    ),
                    source_version=sha,
                    path=rel_path,
                    external_id=str(page["id"]) if page.get("id") is not None else None,
                )
            )
        for child in page.get("subPages") or []:
            self._walk(child, refs, sha)

    async def list_sources(self) -> list[SourceRef]:
        async with self._new_client() as client:
            sha = await self._resolve_version(client)
            tree = await client.get_json(
                self._path(f"/_apis/wiki/wikis/{self._wiki}/pages"),
                params={
                    "path": "/",
                    "recursionLevel": "full",
                    "api-version": _API_VERSION,
                },
            )
        refs: list[SourceRef] = []
        self._walk(tree, refs, sha)
        # Dedupe by path (a repeated subPage node must not double-build a page) and
        # impose a stable order regardless of the API's traversal order (connectors rule).
        by_path = {ref.path: ref for ref in refs}
        refs = sorted(by_path.values(), key=lambda ref: ref.path or "")
        logger.info(
            "event=ado_wiki_listed org=%s project=%s wiki=%s sha=%s pages=%d",
            self._org,
            self._project,
            self._wiki,
            sha,
            len(refs),
        )
        return refs

    async def fetch_text(self, source: SourceRef) -> str:
        # Page content is untrusted data — returned verbatim, never interpreted.
        # SourceRef.path is slash-relative (see _walk); the ADO API wants a leading slash.
        rel = source.path or ""
        path = rel if rel.startswith("/") else f"/{rel}"
        async with self._new_client() as client:
            page = await client.get_json(
                self._path(f"/_apis/wiki/wikis/{self._wiki}/pages"),
                params={
                    "path": path,
                    "includeContent": "true",
                    "api-version": _API_VERSION,
                },
            )
        content = page.get("content")
        if not isinstance(content, str):
            raise HttpFetchError(
                f"ado wiki page {path!r} in {self._org}/{self._project} returned no content"
            )
        logger.info(
            "event=ado_wiki_fetched org=%s project=%s wiki=%s path=%s bytes=%d",
            self._org,
            self._project,
            self._wiki,
            path,
            len(content.encode("utf-8")),
        )
        return content


__all__ = ["AdoWikiBackend"]
