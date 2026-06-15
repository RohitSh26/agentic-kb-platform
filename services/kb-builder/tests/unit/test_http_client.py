"""http_client: retry/backoff on 429 + 5xx, Retry-After, no token in logs/fields."""

import httpx
import pytest

from agentic_kb_builder.connectors.http_client import AsyncHttpClient, HttpFetchError


async def _no_sleep(_seconds: float) -> None:  # keep tests fast and deterministic
    return None


async def test_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", _no_sleep)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={})
        return httpx.Response(200, json={"ok": True})

    client = AsyncHttpClient(
        base_url="https://api.example.com", transport=httpx.MockTransport(handler)
    )
    async with client:
        data = await client.get_json("/thing")
    assert data == {"ok": True}
    assert len(calls) == 2  # one 429, one success


async def test_honors_retry_after_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", record_sleep)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"}, json={})
        return httpx.Response(200, json={})

    client = AsyncHttpClient(transport=httpx.MockTransport(handler))
    async with client:
        await client.get_json("https://api.example.com/x")
    assert slept == [0.25]  # used the Retry-After header, not computed backoff


async def test_retries_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", _no_sleep)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"done": 1})

    client = AsyncHttpClient(transport=httpx.MockTransport(handler))
    async with client:
        data = await client.get_json("https://api.example.com/y")
    assert data == {"done": 1}
    assert len(calls) == 3


async def test_exhausts_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = AsyncHttpClient(transport=httpx.MockTransport(handler), max_retries=2)
    async with client:
        with pytest.raises(HttpFetchError):
            await client.get_json("https://api.example.com/z")


async def test_non_retryable_4xx_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", _no_sleep)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    client = AsyncHttpClient(transport=httpx.MockTransport(handler))
    async with client:
        # The 404 message must name the likely cause (private resource / token access),
        # not just the bare status — most provider 404s are an auth/visibility problem.
        with pytest.raises(HttpFetchError, match=r"private.*token cannot access"):
            await client.get_json("https://api.example.com/missing")
    assert len(calls) == 1  # 404 is not retried


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "credentials"), (403, "scope"), (404, "token cannot access"), (418, "returned 418")],
)
async def test_error_message_hints_name_the_likely_cause(
    monkeypatch: pytest.MonkeyPatch, status: int, needle: str
) -> None:
    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", _no_sleep)
    client = AsyncHttpClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(status))
    )
    async with client:
        with pytest.raises(HttpFetchError, match=needle):
            await client.get_json("https://api.example.com/x")


async def test_token_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", _no_sleep)
    secret = "ghp_SUPERSECRET_TOKEN_VALUE"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {secret}"
        return httpx.Response(200, json={"ok": 1})

    client = AsyncHttpClient(auth_header=f"Bearer {secret}", transport=httpx.MockTransport(handler))
    with caplog.at_level("INFO"):
        async with client:
            await client.get_json("https://api.example.com/secure")
    assert secret not in caplog.text


async def test_get_text_strict_utf8_decode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="héllo".encode())

    client = AsyncHttpClient(transport=httpx.MockTransport(handler))
    async with client:
        text = await client.get_text("https://api.example.com/t")
    assert text == "héllo"


async def test_clamps_huge_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("agentic_kb_builder.connectors.http_client.asyncio.sleep", record_sleep)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "999999"}, json={})
        return httpx.Response(200, json={})

    client = AsyncHttpClient(transport=httpx.MockTransport(handler), backoff_cap=30.0)
    async with client:
        await client.get_json("https://api.example.com/x")
    # A malicious/huge Retry-After is clamped to the backoff cap, never slept whole.
    assert slept == [30.0]
