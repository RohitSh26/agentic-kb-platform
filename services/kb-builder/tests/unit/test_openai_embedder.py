"""OpenAIEmbedder: calls /v1/embeddings, returns the vector, requires an API key."""

import httpx
import pytest

from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.embeddings.openai_embedder import OpenAIEmbedder


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_embed_posts_v1_embeddings_shape_and_returns_vector() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    embedder = OpenAIEmbedder(model="text-embedding-3-small", api_key="k", client=_client(handler))
    result = await embedder.embed("budget enforcement")

    assert result.vector == [0.1, 0.2, 0.3]
    assert result.embedding_hash == content_hash("budget enforcement")
    assert captured["url"] == "https://api.openai.com/v1/embeddings"
    assert captured["body"] == {"model": "text-embedding-3-small", "input": "budget enforcement"}
    await embedder.aclose()


async def test_api_key_becomes_a_bearer_header_on_the_built_client() -> None:
    # No injected client here (unlike the other tests): this is the ONE path that
    # actually exercises HttpEmbedder building its own client from the headers arg.
    embedder = OpenAIEmbedder(api_key="secret-key")
    try:
        assert embedder._client.headers["authorization"] == "Bearer secret-key"
    finally:
        await embedder.aclose()


async def test_missing_api_key_fails_at_construction() -> None:
    with pytest.raises(RuntimeError, match="EMBEDDINGS_API_KEY is required"):
        OpenAIEmbedder(api_key=None, client=_client(lambda _req: httpx.Response(200)))


async def test_missing_api_key_env_fallback_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDINGS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EMBEDDINGS_API_KEY is required"):
        OpenAIEmbedder.from_env()


async def test_embed_raises_on_http_error() -> None:
    embedder = OpenAIEmbedder(api_key="k", client=_client(lambda _req: httpx.Response(500)))
    with pytest.raises(RuntimeError, match="returned 500"):
        await embedder.embed("x")
    await embedder.aclose()


async def test_embed_raises_on_empty_data() -> None:
    embedder = OpenAIEmbedder(
        api_key="k", client=_client(lambda _req: httpx.Response(200, json={"data": []}))
    )
    with pytest.raises(RuntimeError, match="no data"):
        await embedder.embed("x")
    await embedder.aclose()


async def test_embed_raises_on_empty_vector() -> None:
    embedder = OpenAIEmbedder(
        api_key="k",
        client=_client(lambda _req: httpx.Response(200, json={"data": [{"embedding": []}]})),
    )
    with pytest.raises(RuntimeError, match="no vector"):
        await embedder.embed("x")
    await embedder.aclose()


async def test_model_and_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDINGS_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "http://example:9999/v1/")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"embedding": [1.0]}]})

    embedder = OpenAIEmbedder(api_key="k", client=_client(handler))
    assert embedder.embedding_model == "text-embedding-3-large"
    await embedder.embed("q")
    assert seen["url"] == "http://example:9999/v1/embeddings"  # trailing slash trimmed
    await embedder.aclose()
