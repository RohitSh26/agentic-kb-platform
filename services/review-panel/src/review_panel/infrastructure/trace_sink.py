"""TraceSink port + Span DTO (ADR-0032): Postgres-first tracing behind a small port.

Deliberately duplicated from mcp-server's own `trace_sink.py` (ADR-0008 — never shared).
Emit-after-completion semantics ONLY — a ``Span`` is built from a start/end window the caller has
already measured; there is no start_span()/end_span() pairing to keep alive across an ``await``.
Spans are pure observability, never control flow, and are never part of ``PanelState`` — the
LangGraph checkpointer persists only graph state, never trace data, so tracing cannot affect (or
be affected by) crash-resume semantics (docs/contracts/review-panel.md "Tracing").

Never carry secrets, raw prompts, PR/diff text, or KB context in ``attributes`` — the
``Span.__post_init__`` guard below rejects a fixed set of forbidden keys at construction, a
coding-time contract, not a request-time one (docs/contracts/tracing.md "No-content rule").
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from review_panel.structured_logging import get_logger

logger = get_logger("review_panel.infrastructure.trace_sink")

SpanStatus = Literal["ok", "error"]

#: Attribute keys that would leak untrusted/raw content into a span.
FORBIDDEN_ATTRIBUTE_KEYS = frozenset(
    {
        "query_text",
        "normalized_query",
        "task_description",
        "query",
        "prompt",
        "body_text",
        "content",
        "diff",
        "pr_body",
        "pr_title",
        "kb_context",
    }
)


@dataclass(frozen=True)
class Span:
    """One completed unit of work: the whole draft-run attempt, or one graph node."""

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
        implementation to self-swallow.
        """
        ...


async def emit_span(sink: TraceSink, span: Span) -> None:
    """The ONE sanctioned call path for recording a span (ADR-0032 §3, fail-soft, always).

    Any sink exception is caught here, logged as a structured warning (name/service/status
    only), and the span is dropped. Callers never need their own try/except.
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
    """Hermetic test double: records every span it receives, in order."""

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
