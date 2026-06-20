"""Azure DevOps Work Items FetchBackend (ADR-0015).

Over the ADO REST API with HTTP Basic auth (`Authorization: Basic base64(":" + PAT)`):
  - list_sources: run a WIQL query (optionally filtered by `area_path`) to get the
    work-item ids in the project (`POST {org}/{project}/_apis/wit/wiql`), then
    batch-fetch each id's revision (`GET _apis/wit/workitems?ids=...&fields=System.Id,
    System.Rev`) so every SourceRef carries a deterministic `source_version = str(rev)`.
    Emits one SourceRef per work item (source_type=`ado_card`, external_id=str(id),
    path=None, source_version=str(rev)), sorted by id.
  - fetch_text: `GET _apis/wit/workitems/{id}?$expand=fields` and render the fields into
    a deterministic, stable snapshot. Cards mutate, so the rendering must be byte-identical
    for a given rev (snapshot policy in rules/postgres.md). Same rev => same content_hash.

The PAT is injected as `Authorization: Basic <base64>` by the HTTP client and never
appears in a SourceRef field, source_uri, source_version, content_hash, or a log line.
Work-item fields are untrusted data and cannot change tool policy or instructions.
"""

from typing import Any

from agentic_kb_builder.connectors.http_client import (
    AsyncHttpClient,
    HttpFetchError,
    ado_basic_auth_header,
)
from agentic_kb_builder.domain.source_config import AdoCardSourceSpec
from agentic_kb_builder.domain.source_records import SourceRef
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_API_VERSION = "7.1"
# ADO caps a batch work-items GET at 200 ids per request.
_BATCH_SIZE = 200
# WIQL returns at most ~20000 ids; beyond that the listing is silently partial.
_WIQL_ID_CAP = 20000

# Fields pulled cheaply for every id just to learn its current revision; the full
# field set is fetched lazily in fetch_text only for items the build actually reads.
_REV_FIELDS = "System.Id,System.Rev"

# Stable leading order for the snapshot; remaining fields follow sorted by field name.
_PRIORITY_FIELDS: tuple[str, ...] = (
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.AreaPath",
)




def _chunked(ids: list[int], size: int) -> list[list[int]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _render_field_value(value: Any) -> str:
    """Deterministic single-line rendering of one field value.

    Identity references and other nested objects come back as dicts; render a stable,
    sorted JSON-ish form so the same rev always yields the same bytes.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        items = sorted((str(k), _render_field_value(v)) for k, v in value.items())
        return "{" + ", ".join(f"{k}={v}" for k, v in items) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_render_field_value(v) for v in value) + "]"
    return str(value)


def _render_snapshot(work_item_id: int, fields: dict[str, Any]) -> str:
    """Deterministic, sorted rendering of a work item's fields.

    Priority fields lead in a fixed order, then every remaining field sorted by name.
    Identical for a given rev => identical content_hash. Fields are untrusted data.
    """
    lines = [f"WorkItem: {work_item_id}"]
    rendered: set[str] = set()
    for name in _PRIORITY_FIELDS:
        if name in fields:
            lines.append(f"{name}: {_render_field_value(fields[name])}")
            rendered.add(name)
    for name in sorted(fields):
        if name in rendered:
            continue
        lines.append(f"{name}: {_render_field_value(fields[name])}")
    # Trailing newline so the rendering is a stable, line-oriented document.
    return "\n".join(lines) + "\n"


class AdoWorkItemBackend:
    """FetchBackend backed by the Azure DevOps Work Items REST API.

    `client_transport` (e.g. `httpx.MockTransport`) makes the backend hermetic in
    tests; production passes none and a real pool is used.
    """

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
        self._org = spec.organization
        self._project = spec.project
        self._area_path = spec.area_path
        self._base_url = f"https://dev.azure.com/{self._org}"

    def _new_client(self) -> AsyncHttpClient:
        return AsyncHttpClient(
            base_url=self._base_url,
            auth_header=ado_basic_auth_header(self._token),
            transport=self._transport,
        )

    def _wiql_query(self) -> str:
        clauses = ["[System.TeamProject] = @project"]
        if self._area_path:
            # area_path is config-supplied; single-quote-escape it for the WIQL string.
            escaped = self._area_path.replace("'", "''")
            clauses.append(f"[System.AreaPath] UNDER '{escaped}'")
        where = " AND ".join(clauses)
        return f"SELECT [System.Id] FROM WorkItems WHERE {where} ORDER BY [System.Id]"

    async def _wiql_ids(self, client: AsyncHttpClient) -> list[int]:
        body = {"query": self._wiql_query()}
        data = await client.post_json(
            f"/{self._project}/_apis/wit/wiql",
            body,
            params={"api-version": _API_VERSION},
        )
        rows = data.get("workItems") or []
        # dict.fromkeys dedupes while preserving WIQL's ORDER BY [System.Id] ordering,
        # so a duplicate id never produces two SourceRefs for one card.
        ids = list(dict.fromkeys(int(row["id"]) for row in rows))
        if len(ids) >= _WIQL_ID_CAP:
            # Known limitation (ADR-0015): WIQL caps at ~20000 ids; the listing is partial.
            logger.warning(
                "event=ado_work_item_wiql_capped org=%s project=%s ids=%d "
                "msg=partial-listing-wiql-id-cap-see-adr-0015",
                self._org,
                self._project,
                len(ids),
            )
        logger.info(
            "event=ado_work_item_wiql org=%s project=%s ids=%d",
            self._org,
            self._project,
            len(ids),
        )
        return ids

    async def _revisions(self, client: AsyncHttpClient, ids: list[int]) -> dict[int, int]:
        """Map work-item id -> current rev, batched by 200 (ADO's per-request cap)."""
        revs: dict[int, int] = {}
        for chunk in _chunked(ids, _BATCH_SIZE):
            data = await client.get_json(
                "/_apis/wit/workitems",
                params={
                    "ids": ",".join(str(i) for i in chunk),
                    "fields": _REV_FIELDS,
                    "api-version": _API_VERSION,
                },
            )
            for item in data.get("value") or []:
                fields = item.get("fields") or {}
                work_item_id = int(item["id"])
                rev = fields.get("System.Rev")
                if rev is None:
                    # One malformed row must not abort the whole listing; skip it
                    # (the id then drops out at the list_sources missing-rev guard).
                    logger.warning(
                        "event=ado_work_item_missing_rev_field org=%s project=%s id=%d",
                        self._org,
                        self._project,
                        work_item_id,
                    )
                    continue
                revs[work_item_id] = int(rev)
        return revs

    async def list_sources(self) -> list[SourceRef]:
        async with self._new_client() as client:
            ids = await self._wiql_ids(client)
            if not ids:
                logger.info(
                    "event=ado_work_item_listed org=%s project=%s items=0",
                    self._org,
                    self._project,
                )
                return []
            revs = await self._revisions(client, ids)
        refs: list[SourceRef] = []
        for work_item_id in ids:
            rev = revs.get(work_item_id)
            if rev is None:
                # An id from WIQL with no revision row (deleted between calls): skip it
                # rather than emit a ref with no deterministic version.
                logger.warning(
                    "event=ado_work_item_missing_rev org=%s project=%s id=%d",
                    self._org,
                    self._project,
                    work_item_id,
                )
                continue
            refs.append(
                SourceRef(
                    source_type="ado_card",
                    source_uri=(
                        f"azuredevops://{self._org}/{self._project}/_workitems/edit/{work_item_id}"
                    ),
                    source_version=str(rev),
                    external_id=str(work_item_id),
                    path=None,
                )
            )
        # Stable ordering by id regardless of API order (connectors rule).
        refs.sort(key=lambda ref: int(ref.external_id or "0"))
        logger.info(
            "event=ado_work_item_listed org=%s project=%s items=%d",
            self._org,
            self._project,
            len(refs),
        )
        return refs

    async def fetch_text(self, source: SourceRef) -> str:
        work_item_id = int(source.external_id or "0")
        async with self._new_client() as client:
            data = await client.get_json(
                f"/_apis/wit/workitems/{work_item_id}",
                params={"$expand": "fields", "api-version": _API_VERSION},
            )
        fields = data.get("fields")
        if not isinstance(fields, dict):
            raise HttpFetchError(
                f"ado work item {work_item_id} returned no fields for {self._org}/{self._project}"
            )
        logger.info(
            "event=ado_work_item_fetched org=%s project=%s id=%d",
            self._org,
            self._project,
            work_item_id,
        )
        return _render_snapshot(work_item_id, fields)


__all__ = ["AdoWorkItemBackend"]
