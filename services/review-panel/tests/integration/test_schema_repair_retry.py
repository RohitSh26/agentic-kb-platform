"""End-to-end schema-repair retry (evaluation-system.md §2's "adopted" bounded
runtime retry against a machine-checkable validator): one lens's first output
fails review_findings_v1 validation; the run either recovers (one bounded retry)
or fails exactly as it did before this feature existed (two consecutive
failures). Complements the unit-level seam tests in tests/unit/test_schema_repair.py
by proving the retry integrates cleanly with the real compiled graph and its
fan-out concurrency.
"""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from panel_test_support import (
    ScriptedModelClient,
    findings_json,
    key_of,
    make_deps,
    make_pr,
    panel_input,
    thread_config,
)

from review_panel.domain.errors import ReviewerOutputError
from review_panel.graph.build import build_panel_graph

GOOD = findings_json()
BAD_PROSE = "Sure thing! Approved, looks great, no issues at all."


async def test_one_lens_recovers_after_one_retry_and_the_run_completes_normally() -> None:
    pr = make_pr()
    model = ScriptedModelClient({"bug": [BAD_PROSE, GOOD]})
    deps, _, _, store = make_deps(pr=pr, model=model)
    graph = build_panel_graph(deps, InMemorySaver())

    result = await graph.ainvoke(panel_input(pr), thread_config(pr))

    assert model.calls_for("bug") == 2  # exactly one extra call -- the bounded retry
    # every OTHER lens (+ the synthesizer) needed no repair: exactly one call each
    assert model.calls_for("security") == 1
    assert model.calls_for("quality") == 1
    assert model.calls_for("test_coverage") == 1
    assert model.calls_for("synthesizer") == 1
    assert await store.get(key_of(pr)) == result["draft"]  # normal completion, stored


async def test_two_consecutive_failures_end_the_run_exactly_as_before() -> None:
    pr = make_pr()
    model = ScriptedModelClient({"bug": [BAD_PROSE, BAD_PROSE]})
    deps, _, _, store = make_deps(pr=pr, model=model)
    graph = build_panel_graph(deps, InMemorySaver())

    with pytest.raises(ReviewerOutputError):
        await graph.ainvoke(panel_input(pr), thread_config(pr))

    assert model.calls_for("bug") == 2  # bounded: no third attempt
    assert await store.get(key_of(pr)) is None  # nothing unvalidated is ever stored
