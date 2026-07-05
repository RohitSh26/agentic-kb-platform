"""TraceSink port (ADR-0032): fail-soft boundary, no-content guard, port swap. Hermetic."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from panel_test_support import RaisingTraceSink

from review_panel.infrastructure.trace_sink import (
    InMemoryTraceSink,
    NullTraceSink,
    Span,
    emit_span,
)


def _span(**overrides: object) -> Span:
    started = datetime(2026, 7, 5, tzinfo=UTC)
    defaults: dict[str, object] = {
        "trace_id": "acme/platform#7@abc123",
        "span_id": uuid.uuid4(),
        "parent_span_id": None,
        "name": "load_pr",
        "service": "review-panel",
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
    ["query_text", "task_description", "prompt", "body_text", "diff", "pr_body", "kb_context"],
)
def test_span_rejects_content_bearing_attribute_keys(forbidden_key: str) -> None:
    with pytest.raises(ValueError, match="content fields"):
        _span(attributes={forbidden_key: "whatever"})


def test_span_accepts_aggregate_only_attributes() -> None:
    span = _span(attributes={"lens": "bug", "findings": 2, "verdict": "request_changes"})
    assert span.attributes == {"lens": "bug", "findings": 2, "verdict": "request_changes"}


# --------------------------------------------------------------------------- fail-soft


async def test_emit_span_swallows_a_raising_sink() -> None:
    await emit_span(RaisingTraceSink(), _span())  # must not raise


async def test_null_trace_sink_drops_every_span_silently() -> None:
    await emit_span(NullTraceSink(), _span())  # nothing to assert beyond "did not raise"


# --------------------------------------------------------------------------- port swap


async def test_in_memory_trace_sink_records_spans_in_order() -> None:
    sink = InMemoryTraceSink()
    first, second = _span(name="load_pr"), _span(name="review_bug")

    await emit_span(sink, first)
    await emit_span(sink, second)

    assert [span.name for span in sink.spans] == ["load_pr", "review_bug"]
