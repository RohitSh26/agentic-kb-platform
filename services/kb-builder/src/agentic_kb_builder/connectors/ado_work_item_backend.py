"""Azure DevOps Work Items FetchBackend — STUB (filled in by the ADO Work Items PR).

Intended approach (ADR-0015), over the ADO REST API with HTTP Basic auth
(`Authorization: Basic base64(":" + PAT)`):
  - list_sources: run a WIQL query built from the spec (area_path, work_item_types,
    states, tags) -> work-item ids (`POST {org}/{project}/_apis/wit/wiql`), then emit
    one SourceRef per id (source_type=`ado_card`, external_id=id, source_version=rev).
  - fetch_text: `GET .../wit/workitems/{id}?$expand=fields`, normalize the fields into
    a deterministic snapshot text (cards mutate, so the rendering must be stable for a
    given rev — snapshot policy in rules/postgres.md). source_version = the work-item `rev`.

This class exists so `production_backend_factory` imports cleanly; the follow-up PR
replaces the NotImplementedError bodies.
"""

from typing import Any

from agentic_kb_builder.domain.source_config import AdoCardSourceSpec
from agentic_kb_builder.domain.source_records import SourceRef


class AdoWorkItemBackend:
    """STUB FetchBackend for ADO Work Items; methods raise until the ADO Work Items PR."""

    def __init__(
        self,
        spec: AdoCardSourceSpec,
        token: str | None,
        *,
        client_transport: Any | None = None,
    ) -> None:
        self._spec = spec
        self._token = token
        self._transport = client_transport

    async def list_sources(self) -> list[SourceRef]:
        raise NotImplementedError("filled in the ADO Work Items PR")

    async def fetch_text(self, source: SourceRef) -> str:
        raise NotImplementedError("filled in the ADO Work Items PR")


__all__ = ["AdoWorkItemBackend"]
