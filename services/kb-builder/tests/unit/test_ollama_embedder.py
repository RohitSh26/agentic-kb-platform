"""OllamaEmbedder: calls /api/embeddings, returns the vector, never logs/sends junk."""

import httpx
import pytest

from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.embeddings.ollama_embedder import OllamaEmbedder


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_embed_returns_vector_and_content_hash() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})

    embedder = OllamaEmbedder(model="nomic-embed-text", client=_client(handler))
    result = await embedder.embed("budget enforcement")

    assert result.vector == [0.1, 0.2, 0.3]
    assert result.embedding_hash == content_hash("budget enforcement")
    # The prompt+model are sent; nothing else leaks into the request body.
    assert captured == {"model": "nomic-embed-text", "prompt": "budget enforcement"}
    await embedder.aclose()


async def test_embed_raises_on_http_error() -> None:
    embedder = OllamaEmbedder(client=_client(lambda _req: httpx.Response(500)))
    with pytest.raises(RuntimeError, match="returned 500"):
        await embedder.embed("x")
    await embedder.aclose()


async def test_embed_raises_on_empty_vector() -> None:
    embedder = OllamaEmbedder(
        client=_client(lambda _req: httpx.Response(200, json={"embedding": []}))
    )
    with pytest.raises(RuntimeError, match="no vector"):
        await embedder.embed("x")
    await embedder.aclose()


async def test_model_and_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDINGS_MODEL", "mxbai-embed-large")
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "http://example:9999/")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"embedding": [1.0]})

    embedder = OllamaEmbedder(client=_client(handler))
    assert embedder.embedding_model == "mxbai-embed-large"
    await embedder.embed("q")
    assert seen["url"] == "http://example:9999/api/embeddings"  # trailing slash trimmed
    await embedder.aclose()
