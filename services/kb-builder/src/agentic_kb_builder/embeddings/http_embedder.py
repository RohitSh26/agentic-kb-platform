"""Shared HTTP plumbing for embedders backed by a remote embeddings endpoint.

Both `OllamaEmbedder` (Ollama's native `/api/embeddings` shape) and `OpenAIEmbedder`
(the OpenAI-compatible `/v1/embeddings` shape) hold one `httpx.AsyncClient` for the
build's lifetime and release it via `aclose()`. `EmbeddingSimilarityProvider` closes
any embedder that IS one of these (`isinstance(embedder, HttpEmbedder)`) — never the
zero-cost `LocalHashEmbedder`, which opens no HTTP client. A new HTTP-backed provider
subclasses this instead of re-implementing client lifecycle management.
"""

import httpx

_TIMEOUT = 60.0


class HttpEmbedder:
    """Base class owning the long-lived `httpx.AsyncClient` an HTTP embedder posts through.

    Subclasses resolve their own base_url/model/headers, then call
    `super().__init__(client=client, headers=headers)`. An injected `client` (tests, via
    `httpx.MockTransport`) is used verbatim; otherwise a real pooled client is built with
    the given headers (e.g. an `Authorization: Bearer ...` auth header).
    """

    def __init__(self, *, client: httpx.AsyncClient | None, headers: dict[str, str]) -> None:
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["HttpEmbedder"]
