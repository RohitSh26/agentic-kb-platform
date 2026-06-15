"""Async HTTP client used *inside* the FetchBackend boundary (ADR-0015).

This is the only place a connector path makes a real network request. It wraps
`httpx.AsyncClient` the same way the Azure SDKs sit behind SearchClient/ModelClient:
production backends depend on this small surface, never on httpx directly, so the
backends stay swappable and tests stay hermetic (inject a fake transport).

Responsibilities:
- inject an auth header (GitHub Bearer / ADO Basic) — resolved by the caller, never built here;
- `get_json` / `get_text` with a bounded timeout;
- bounded retry/backoff on 429 (honoring Retry-After) and 5xx;
- strict UTF-8 decode so content hashes never diverge across machines;
- structured logging `event=http_fetch_*` that NEVER contains the token.
"""

import asyncio
from types import TracebackType
from typing import Any

import httpx

from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 4
_DEFAULT_BACKOFF_BASE = 0.5  # seconds; doubled each attempt, capped
_DEFAULT_BACKOFF_CAP = 8.0
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpFetchError(Exception):
    """A request failed permanently (non-retryable status, or retries exhausted)."""


class AsyncHttpClient:
    """Thin async wrapper over httpx with auth injection + bounded retry/backoff.

    Pass `transport` (e.g. `httpx.MockTransport`) in tests; production passes none
    and a real connection pool is used. `auth_header` is the fully-formed header
    value (e.g. ``"Bearer <pat>"``) — this class never sees the raw env var name
    and never logs the header.
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        auth_header: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        backoff_cap: float = _DEFAULT_BACKOFF_CAP,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers: dict[str, str] = {"Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        if auth_header is not None:
            headers["Authorization"] = auth_header
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        # follow_redirects stays False (httpx default, pinned explicitly): this is an
        # auth-bearing client, and a redirect could forward the Authorization header
        # to an unintended host. A 3xx surfaces as a non-2xx and is handled by _request.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> "AsyncHttpClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            # HTTP-date form is not honored here; fall back to computed backoff.
            return None

    def _backoff_seconds(self, attempt: int) -> float:
        return min(self._backoff_base * (2**attempt), self._backoff_cap)

    async def _request(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        # `url` (path) is logged; query params are NOT logged (could embed creds).
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(url, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    logger.warning(
                        "event=http_fetch_error url=%s attempt=%d error=%s",
                        url,
                        attempt,
                        type(exc).__name__,
                    )
                    raise HttpFetchError(f"GET {url} failed: {exc}") from exc
                delay = self._backoff_seconds(attempt)
                logger.warning(
                    "event=http_fetch_retry url=%s attempt=%d reason=transport delay=%.2f",
                    url,
                    attempt,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                retry_after = self._retry_after_seconds(response)
                # Clamp a server-supplied Retry-After to the backoff cap so a huge
                # (mis)configured value can never stall the nightly build.
                delay = (
                    min(retry_after, self._backoff_cap)
                    if retry_after is not None
                    else self._backoff_seconds(attempt)
                )
                logger.warning(
                    "event=http_fetch_retry url=%s attempt=%d status=%d delay=%.2f",
                    url,
                    attempt,
                    response.status_code,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                logger.warning(
                    "event=http_fetch_failed url=%s status=%d",
                    url,
                    response.status_code,
                )
                raise HttpFetchError(f"GET {url} returned {response.status_code}")

            logger.info(
                "event=http_fetch_ok url=%s status=%d bytes=%d",
                url,
                response.status_code,
                len(response.content),
            )
            return response

        # Loop only exits via return/raise above, except when retries are exhausted
        # on a retryable status (the final attempt falls through to here).
        raise HttpFetchError(f"GET {url} exhausted {self._max_retries} retries") from last_exc

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request(url, params)
        return response.json()

    async def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        response = await self._request(url, params)
        # Strict UTF-8 so hashes are stable across machines.
        return response.content.decode("utf-8")


__all__ = ["AsyncHttpClient", "HttpFetchError"]
