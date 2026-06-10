"""Connector boundary for the nightly build.

Connectors are deterministic: same source state => same normalized content =>
same content_hash. All network I/O sits behind FetchBackend so tests (and the
build engine) can inject fakes — connectors never touch SDKs directly.
"""

from typing import ClassVar, Protocol

from agentic_kb_builder.domain import NormalizedContent, SourceRef, SourceType
from agentic_kb_builder.domain.content_hasher import content_hash, normalize_text
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


class FetchBackend(Protocol):
    """Thin, fakeable fetch boundary; the only place real I/O may happen.

    Implementations must decode bytes as UTF-8 (errors="strict") so hashes
    never diverge across machines.
    """

    async def list_sources(self) -> list[SourceRef]: ...

    async def fetch_text(self, source: SourceRef) -> str: ...


class Connector(Protocol):
    source_type: ClassVar[SourceType]

    async def list_sources(self) -> list[SourceRef]: ...

    async def fetch(self, source: SourceRef) -> NormalizedContent: ...


class BaseConnector:
    """Shared deterministic normalize+hash pipeline over an injected FetchBackend."""

    source_type: ClassVar[SourceType]

    def __init__(self, backend: FetchBackend) -> None:
        self._backend = backend

    async def list_sources(self) -> list[SourceRef]:
        sources = await self._backend.list_sources()
        for source in sources:
            if source.source_type != self.source_type:
                raise ValueError(
                    f"backend returned source_type={source.source_type!r}, "
                    f"expected {self.source_type!r}"
                )
        return sources

    def _normalize(self, raw: str) -> str:
        return normalize_text(raw)

    async def fetch(self, source: SourceRef) -> NormalizedContent:
        raw = await self._backend.fetch_text(source)
        text = self._normalize(raw)
        digest = content_hash(text)
        logger.info(
            "event=connector_fetch connector=%s source_uri=%s source_version=%s content_hash=%s",
            self.source_type,
            source.source_uri,
            source.source_version,
            digest,
        )
        return NormalizedContent(source=source, text=text, content_hash=digest)
