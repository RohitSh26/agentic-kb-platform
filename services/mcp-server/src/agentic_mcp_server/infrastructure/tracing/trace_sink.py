"""TraceSink port + Span DTO (ADR-0032): Postgres-first tracing behind a small port.

Emit-after-completion semantics ONLY — a ``Span`` is built from a start/end window the caller has
already measured; there is no start_span()/end_span() pairing to keep alive across an ``await``.
Spans are pure observability, never control flow: emitting one never blocks, delays materially, or
fails the call it describes. Fail-soft is enforced ONCE, centrally, by ``emit_span`` below — never
by requiring every concrete sink to self-swallow (see docs/contracts/tracing.md).

Never carry secrets, raw prompts, document bodies, or query text in ``attributes`` — the
``Span.__post_init__`` guard below rejects a fixed set of forbidden keys at construction, a
coding-time contract, not a request-time one.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

SpanStatus = Literal["ok", "error"]

#: Attribute keys that would leak untrusted/raw content into a span (docs/contracts/tracing.md
#: "No-content rule" — the same aggregate-only posture as the ADR-0014 dashboard).
FORBIDDEN_ATTRIBUTE_KEYS = frozenset(
    {
        "query_text",
        "normalized_query",
        "task_description",
        "query",
        "prompt",
        "body_text",
        "content",
    }
)


@dataclass(frozen=True)
class Span:
    """One completed unit of work: a whole tool call, or one node inside its graph."""

    trace_id: str
    span_id: uuid.UUID
    name: str
    service: str
    started_at: datetime
    ended_at: datetime
    status: SpanStatus
    parent_span_id: uuid.UUID | None = None
    attributes: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

    def __post_init__(self) -> None:
        leaked = FORBIDDEN_ATTRIBUTE_KEYS & self.attributes.keys()
        if leaked:
            raise ValueError(f"span attributes must never carry content fields: {sorted(leaked)}")


class TraceSink(Protocol):
    async def emit(self, span: Span) -> None:
        """Persist one completed span.

        A concrete adapter MAY raise on a genuine failure — the fail-soft contract is
        enforced at the ``emit_span`` call boundary below, not by requiring every
        implementation to self-swallow (that is also how a deliberately-raising fake
        proves the boundary holds regardless of which adapter sits behind the port).
        """
        ...


async def emit_span(sink: TraceSink, span: Span) -> None:
    """The ONE sanctioned call path for recording a span (ADR-0032 §3, fail-soft, always).

    Any sink exception is caught here, logged as a structured warning (name/service/status
    only — never ``attributes``, which could carry request-shaped data even though it must
    never carry raw content), and the span is dropped. Callers never need their own
    try/except around a trace emission.
    """
    try:
        await sink.emit(span)
    except Exception:
        logger.warning(
            "event=trace_sink_error name=%s service=%s status=%s",
            span.name,
            span.service,
            span.status,
        )


class NullTraceSink:
    """The ``TRACE_SINK=none`` / no-database default: spans are measured, then dropped."""

    async def emit(self, span: Span) -> None:
        return None


@dataclass
class InMemoryTraceSink:
    """Hermetic test double + local-dev fake: records every span it receives, in order."""

    spans: list[Span] = field(default_factory=lambda: list[Span]())

    async def emit(self, span: Span) -> None:
        self.spans.append(span)


__all__ = [
    "FORBIDDEN_ATTRIBUTE_KEYS",
    "InMemoryTraceSink",
    "NullTraceSink",
    "Span",
    "SpanStatus",
    "TraceSink",
    "emit_span",
]
