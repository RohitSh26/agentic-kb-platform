"""AdoWorkItemBackend against a fake httpx transport — no real network.

Asserts: WIQL POST + Basic auth header; batch revisions drive a deterministic
source_version=rev; refs are source_type=ado_card with external_id and path=None;
source_version stable across two runs; listing sorted by id; fetch_text returns a
deterministic, byte-identical normalized snapshot; the PAT never leaks into any
SourceRef field or log line. Also covers post_json retry + token-safety.
"""

import base64

import httpx
import pytest

from agentic_kb_builder.connectors.ado_work_item_backend import AdoWorkItemBackend
from agentic_kb_builder.connectors.http_client import AsyncHttpClient, HttpFetchError
from agentic_kb_builder.domain.source_config import AdoCardSourceSpec

_TOKEN = "ADO_FAKE_PAT_DO_NOT_LOG"
_ORG = "contoso"
_PROJECT = "Widgets"
_EXPECTED_BASIC = "Basic " + base64.b64encode(f":{_TOKEN}".encode()).decode("ascii")

# base_url is https://dev.azure.com/{org}, so every resolved path carries the org prefix.
_WIQL_PATH = f"/{_ORG}/{_PROJECT}/_apis/wit/wiql"
_BATCH_PATH = f"/{_ORG}/_apis/wit/workitems"
_ITEM_PREFIX = f"/{_ORG}/_apis/wit/workitems/"

# Returned by WIQL deliberately OUT of id order, to prove the backend sorts by id.
_WIQL_IDS = [42, 7, 19]
_REVS = {42: 5, 7: 2, 19: 9}

_WORK_ITEM_FIELDS = {
    7: {
        "System.Id": 7,
        "System.Rev": 2,
        "System.Title": "Login button is misaligned",
        "System.State": "Active",
        "System.WorkItemType": "Bug",
        "System.AreaPath": "Widgets\\UI",
        "System.Tags": "frontend; css",
        "System.AssignedTo": {
            "displayName": "Pat Dev",
            "uniqueName": "pat@contoso.com",
        },
    },
}


def _make_handler() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        # Token must always be present as Basic auth, but never in path/query.
        assert request.headers.get("Authorization") == _EXPECTED_BASIC
        assert _TOKEN not in str(request.url)
        path = request.url.path

        if path == _WIQL_PATH:
            assert request.method == "POST"
            body = request.read().decode("utf-8")
            assert _TOKEN not in body
            assert "SELECT [System.Id]" in body
            assert request.url.params.get("api-version") == "7.1"
            return httpx.Response(200, json={"workItems": [{"id": i} for i in _WIQL_IDS]})

        if path == _BATCH_PATH and "ids" in request.url.params:
            ids = [int(x) for x in request.url.params["ids"].split(",")]
            assert request.url.params.get("fields") == "System.Id,System.Rev"
            value = [{"id": i, "fields": {"System.Id": i, "System.Rev": _REVS[i]}} for i in ids]
            return httpx.Response(200, json={"count": len(value), "value": value})

        if path.startswith(_ITEM_PREFIX):
            assert request.url.params.get("$expand") == "fields"
            work_item_id = int(path.removeprefix(_ITEM_PREFIX))
            return httpx.Response(
                200, json={"id": work_item_id, "fields": _WORK_ITEM_FIELDS[work_item_id]}
            )

        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


def _spec(area_path: str | None = None) -> AdoCardSourceSpec:
    return AdoCardSourceSpec(
        type="ado_card",
        name="cards",
        organization=_ORG,
        project=_PROJECT,
        area_path=area_path,
    )


def _backend(area_path: str | None = None) -> AdoWorkItemBackend:
    return AdoWorkItemBackend(_spec(area_path), _TOKEN, client_transport=_make_handler())


async def test_list_sources_emits_ado_card_refs() -> None:
    refs = await _backend().list_sources()
    assert {int(r.external_id or "") for r in refs} == set(_WIQL_IDS)
    for ref in refs:
        work_item_id = int(ref.external_id or "")
        assert ref.source_type == "ado_card"
        assert ref.path is None
        assert ref.source_version == str(_REVS[work_item_id])
        assert ref.source_uri == (f"azuredevops://{_ORG}/{_PROJECT}/_workitems/edit/{work_item_id}")
        # token never embedded in any identity field
        assert _TOKEN not in ref.source_uri
        assert _TOKEN not in ref.source_version
        assert _TOKEN not in (ref.external_id or "")


async def test_list_sources_sorted_by_id() -> None:
    refs = await _backend().list_sources()
    ids = [int(r.external_id or "") for r in refs]
    assert ids == sorted(_WIQL_IDS)


async def test_source_version_stable_across_runs() -> None:
    first = {r.external_id: r.source_version for r in await _backend().list_sources()}
    second = {r.external_id: r.source_version for r in await _backend().list_sources()}
    assert first == second
    assert first == {str(i): str(_REVS[i]) for i in _WIQL_IDS}


async def test_area_path_filters_wiql() -> None:
    import json

    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _WIQL_PATH:
            captured.append(request.read().decode("utf-8"))
            return httpx.Response(200, json={"workItems": []})
        return httpx.Response(404, json={})

    backend = AdoWorkItemBackend(
        _spec(area_path="Widgets\\UI"),
        _TOKEN,
        client_transport=httpx.MockTransport(handler),
    )
    await backend.list_sources()
    assert captured
    # Decode the JSON body so the assertion sees the literal query, not JSON escaping.
    query = json.loads(captured[0])["query"]
    assert "[System.AreaPath] UNDER 'Widgets\\UI'" in query


async def test_no_area_path_omits_area_clause() -> None:
    import json

    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _WIQL_PATH:
            captured.append(request.read().decode("utf-8"))
            return httpx.Response(200, json={"workItems": []})
        return httpx.Response(404, json={})

    backend = AdoWorkItemBackend(_spec(), _TOKEN, client_transport=httpx.MockTransport(handler))
    await backend.list_sources()
    query = json.loads(captured[0])["query"]
    assert "AreaPath" not in query
    assert "[System.TeamProject] = @project" in query


async def test_empty_wiql_returns_no_refs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _WIQL_PATH:
            return httpx.Response(200, json={"workItems": []})
        # No batch call should be made when there are no ids.
        raise AssertionError(f"unexpected request to {request.url.path}")

    backend = AdoWorkItemBackend(_spec(), _TOKEN, client_transport=httpx.MockTransport(handler))
    assert await backend.list_sources() == []


async def test_fetch_text_is_deterministic_snapshot() -> None:
    backend = _backend()
    refs = await backend.list_sources()
    ref = next(r for r in refs if r.external_id == "7")
    first = await backend.fetch_text(ref)
    second = await backend.fetch_text(ref)
    # Byte-identical across calls => stable content_hash for a given rev.
    assert first == second
    # Priority fields lead in fixed order, then remaining fields sorted by name.
    assert first == (
        "WorkItem: 7\n"
        "System.Title: Login button is misaligned\n"
        "System.State: Active\n"
        "System.WorkItemType: Bug\n"
        "System.AreaPath: Widgets\\UI\n"
        "System.AssignedTo: {displayName=Pat Dev, uniqueName=pat@contoso.com}\n"
        "System.Id: 7\n"
        "System.Rev: 2\n"
        "System.Tags: frontend; css\n"
    )


async def test_fetch_text_missing_fields_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1})  # no "fields"

    backend = AdoWorkItemBackend(_spec(), _TOKEN, client_transport=httpx.MockTransport(handler))
    from agentic_kb_builder.domain.source_records import SourceRef

    ref = SourceRef(
        source_type="ado_card",
        source_uri=f"azuredevops://{_ORG}/{_PROJECT}/_workitems/edit/1",
        source_version="1",
        external_id="1",
    )
    with pytest.raises(HttpFetchError):
        await backend.fetch_text(ref)


async def test_basic_auth_header_built_correctly() -> None:
    # Username empty + password = PAT, base64 of ":<token>".
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        if request.url.path == _WIQL_PATH:
            return httpx.Response(200, json={"workItems": []})
        return httpx.Response(404, json={})

    backend = AdoWorkItemBackend(_spec(), _TOKEN, client_transport=httpx.MockTransport(handler))
    await backend.list_sources()
    assert seen and all(h == _EXPECTED_BASIC for h in seen)
    decoded = base64.b64decode(seen[0].removeprefix("Basic ")).decode("utf-8")
    assert decoded == f":{_TOKEN}"


async def test_no_token_in_any_log(caplog: pytest.LogCaptureFixture) -> None:
    backend = _backend()
    with caplog.at_level("INFO"):
        refs = await backend.list_sources()
        await backend.fetch_text(next(r for r in refs if r.external_id == "7"))
    assert _TOKEN not in caplog.text


# --- post_json (added to AsyncHttpClient for the WIQL POST) -------------------


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_post_json_returns_body_and_sends_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.read().decode("utf-8") == '{"query":"x"}'
        return httpx.Response(200, json={"workItems": []})

    client = AsyncHttpClient(transport=httpx.MockTransport(handler))
    async with client:
        data = await client.post_json("https://dev.azure.com/o/_apis/wit/wiql", {"query": "x"})
    assert data == {"workItems": []}


async def test_post_json_retries_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", _no_sleep)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": 1})

    client = AsyncHttpClient(transport=httpx.MockTransport(handler))
    async with client:
        data = await client.post_json("https://dev.azure.com/o/x", {"q": 1})
    assert data == {"ok": 1}
    assert len(calls) == 3


async def test_post_json_never_logs_token_or_body(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == _EXPECTED_BASIC
        return httpx.Response(200, json={"ok": 1})

    client = AsyncHttpClient(auth_header=_EXPECTED_BASIC, transport=httpx.MockTransport(handler))
    with caplog.at_level("INFO"):
        async with client:
            await client.post_json("https://dev.azure.com/o/secure", {"secret": _TOKEN})
    assert _TOKEN not in caplog.text
