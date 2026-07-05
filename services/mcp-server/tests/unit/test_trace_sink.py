"""TraceSink port (ADR-0032): fail-soft boundary, no-content guard, port swap. Hermetic — no DB."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from broker_test_support import RaisingTraceSink

from agentic_mcp_server.infrastructure.tracing.trace_sink import (
    InMemoryTraceSink,
    NullTraceSink,
    Span,
    emit_span,
)


def _span(**overrides: object) -> Span:
    started = datetime(2026, 7, 5, tzinfo=UTC)
    defaults: dict[str, object] = {
        "trace_id": "trace-1",
        "span_id": uuid.uuid4(),
        "parent_span_id": None,
        "name": "resolve_scope",
        "service": "mcp-server",
        "started_at": started,
        "ended_at": started + timedelta(milliseconds=5),
        "status": "ok",
        "attributes": {},
    }
    defaults.update(overrides)
    return Span(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------- no-content rule


@pytest.mark.parametrize(
    "forbidden_key",
    ["query_text", "normalized_query", "task_description", "query", "prompt", "body_text"],
)
def test_span_rejects_content_bearing_attribute_keys(forbidden_key: str) -> None:
    with pytest.raises(ValueError, match="content fields"):
        _span(attributes={forbidden_key: "whatever"})


def test_span_accepts_aggregate_only_attributes() -> None:
    span = _span(attributes={"entities": 3, "ambiguous": 0, "retried": False})
    assert span.attributes == {"entities": 3, "ambiguous": 0, "retried": False}


# --------------------------------------------------------------------------- fail-soft


async def test_emit_span_swallows_a_raising_sink() -> None:
    """The fail-soft boundary: emit_span never propagates a sink's exception."""
    await emit_span(RaisingTraceSink(), _span())  # must not raise


async def test_null_trace_sink_drops_every_span_silently() -> None:
    sink = NullTraceSink()
    await emit_span(sink, _span())  # nothing to assert beyond "did not raise"


# --------------------------------------------------------------------------- port swap


async def test_in_memory_trace_sink_records_spans_in_order() -> None:
    """The Langfuse-later seam: graph/tool code depends only on the TraceSink Protocol,
    so swapping the concrete adapter (here, a fake) changes nothing about the call site."""
    sink = InMemoryTraceSink()
    first, second = _span(name="resolve_scope"), _span(name="blast_radius")

    await emit_span(sink, first)
    await emit_span(sink, second)

    assert [span.name for span in sink.spans] == ["resolve_scope", "blast_radius"]
