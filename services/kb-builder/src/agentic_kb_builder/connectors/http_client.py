"""Async HTTP client used *inside* the FetchBackend boundary.

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
import base64
from types import TracebackType
from typing import Any

import httpx

from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


def ado_basic_auth_header(token: str | None) -> str | None:
    """ADO PAT auth: HTTP Basic with empty username and the PAT as the password.

    Returns ``Basic base64(":" + token)``, or None when no token is configured (the
    source runs unauthenticated). The raw token is encoded here and only here — it is
    never logged and never stored on a SourceRef. Shared by the ADO wiki + work-item
    backends so the encoding cannot diverge between them.
    """
    if not token:
        return None
    encoded = base64.b64encode(f":{token}".encode()).decode("ascii")
    return f"Basic {encoded}"

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 4
_DEFAULT_BACKOFF_BASE = 0.5  # seconds; doubled each attempt, capped
_DEFAULT_BACKOFF_CAP = 8.0
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpFetchError(Exception):
    """A request failed permanently (non-retryable status, or retries exhausted)."""


#: Actionable hints for the auth/visibility statuses that cause most setup pain. The
#: provider (GitHub / Azure DevOps) returns 404 for a private resource a valid token
#: can't see, so a bare "returned 404" is misleading — name the likely cause.
_STATUS_HINTS = {
    401: "bad or expired credentials — check the token value in its auth env var",
    403: "forbidden — token lacks scope, SSO not authorized for the PAT, or rate-limited",
    404: (
        "not found, OR the resource is private and the token cannot access it — "
        "verify the PAT's scope and that it is granted to this repo/org/project"
    ),
}

#: When the request carried NO Authorization header, a 401/403/404 is almost always a
#: *missing auth* config, not a scope problem — the source omitted auth.token_env while
#: pointing at a private resource (a private repo returns 404, not 403). This is the
#: single most time-consuming misconfig, so name it precisely instead of blaming the PAT.
_UNAUTHENTICATED_HINT = (
    "no Authorization header was sent — this source has no auth.token_env configured, so it "
    "runs UNAUTHENTICATED (public resources only); a PRIVATE repo/org/project returns 404. "
    "Add an auth.token_env to this source in your sources.yaml to send a token"
)


def _provider_message(response: httpx.Response) -> str | None:
    """Pull the provider's own error text out of a 4xx/5xx body. GitHub and Azure
    DevOps both return ``{"message": "..."}`` — surfacing it turns a bare "returned
    400" into the actual reason (e.g. "Area path 'Platform\\KB' does not exist"). The
    body is the SERVER's error text, never the request (which could carry creds), so
    it is safe to include; truncate so an HTML error page can't flood the log."""
    try:
        data = response.json()
    except (ValueError, UnicodeDecodeError):
        data = None
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:300]
    text = response.text.strip()
    return text[:300] if text else None


def _http_error_message(
    method: str,
    url: str,
    status: int,
    *,
    authenticated: bool = True,
    provider_message: str | None = None,
) -> str:
    base = f"{method} {url} returned {status}"
    if not authenticated and status in (401, 403, 404):
        hint: str | None = _UNAUTHENTICATED_HINT
    else:
        hint = _STATUS_HINTS.get(status)
    parts = [base]
    if hint:
        parts.append(f"({hint})")
    if provider_message:
        parts.append(f"— server said: {provider_message}")
    return " ".join(parts)


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
        # remember whether we sent a token, so a 401/403/404 can distinguish a
        # missing-auth config from a real scope/visibility problem
        self._authenticated = auth_header is not None
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

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json_body: Any | None = None,
    ) -> httpx.Response:
        # `method` + `url` (path) are logged; query params and JSON bodies are NOT
        # logged (either could embed creds).
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(method, url, params=params, json=json_body)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    logger.warning(
                        "event=http_fetch_error method=%s url=%s attempt=%d error=%s",
                        method,
                        url,
                        attempt,
                        type(exc).__name__,
                    )
                    raise HttpFetchError(f"{method} {url} failed: {exc}") from exc
                delay = self._backoff_seconds(attempt)
                logger.warning(
                    "event=http_fetch_retry method=%s url=%s attempt=%d "
                    "reason=transport delay=%.2f",
                    method,
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
                    "event=http_fetch_retry method=%s url=%s attempt=%d status=%d delay=%.2f",
                    method,
                    url,
                    attempt,
                    response.status_code,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                logger.warning(
                    "event=http_fetch_failed method=%s url=%s status=%d",
                    method,
                    url,
                    response.status_code,
                )
                raise HttpFetchError(
                    _http_error_message(
                        method,
                        url,
                        response.status_code,
                        authenticated=self._authenticated,
                        provider_message=_provider_message(response),
                    )
                )

            logger.info(
                "event=http_fetch_ok method=%s url=%s status=%d bytes=%d",
                method,
                url,
                response.status_code,
                len(response.content),
            )
            return response

        # Loop only exits via return/raise above, except when retries are exhausted
        # on a retryable status (the final attempt falls through to here).
        raise HttpFetchError(f"{method} {url} exhausted {self._max_retries} retries") from last_exc

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", url, params)
        return response.json()

    async def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        response = await self._request("GET", url, params)
        # Strict UTF-8 so hashes are stable across machines.
        return response.content.decode("utf-8")

    async def post_json(
        self,
        url: str,
        json_body: Any,
        params: dict[str, Any] | None = None,
    ) -> Any:
        # Same retry/backoff/token-safety path as get_json; the body is never logged.
        response = await self._request("POST", url, params, json_body=json_body)
        return response.json()


__all__ = ["AsyncHttpClient", "HttpFetchError", "ado_basic_auth_header"]
