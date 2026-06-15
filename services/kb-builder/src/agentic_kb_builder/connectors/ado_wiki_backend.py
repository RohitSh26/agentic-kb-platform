"""Azure DevOps Wiki FetchBackend — STUB (filled in by the ADO Wiki PR).

Intended approach (ADR-0015), all over the ADO REST API with HTTP Basic auth
(`Authorization: Basic base64(":" + PAT)`):
  - list_sources: list pages with `recursionLevel=full`
    (`GET {org}/{project}/_apis/wiki/wikis/{wiki}/pages?recursionLevel=full`),
    emitting one SourceRef per page (source_type=`azure_wiki`, external_id=page id,
    source_version = page version / ETag).
  - fetch_text: fetch the page content
    (`GET .../pages/{id}?includeContent=true`) and return its markdown.

This class exists so `production_backend_factory` imports cleanly; the follow-up PR
replaces the NotImplementedError bodies.
"""

from typing import Any

from agentic_kb_builder.domain.source_config import AzureWikiSourceSpec
from agentic_kb_builder.domain.source_records import SourceRef


class AdoWikiBackend:
    """STUB FetchBackend for Azure DevOps Wiki; methods raise until the ADO Wiki PR."""

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

    async def list_sources(self) -> list[SourceRef]:
        raise NotImplementedError("filled in the ADO Wiki PR")

    async def fetch_text(self, source: SourceRef) -> str:
        raise NotImplementedError("filled in the ADO Wiki PR")


__all__ = ["AdoWikiBackend"]
