"""Per-step tracing (ADR-0032): review-panel's draft-run spans.

Covers the acceptance criteria: one root span (`review_panel.draft_run`) per draft-run
attempt plus one span per graph node that actually ran, all sharing one trace_id (the
draft's own key) and pointing at the root's span_id as parent; a deliberately-raising
sink never fails the draft run; NullTraceSink is the default; a resumed run never
re-emits a span for an already-completed node; and (TEST_DATABASE_URL-gated)
PostgresTraceSink actually lands rows in the dedicated `review_panel` schema.
"""

import dataclasses
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from panel_test_support import (
    TEST_DATABASE_URL,
    RaisingTraceSink,
    key_of,
    make_deps,
    make_pr,
    panel_input,
    thread_config,
)
from psycopg import AsyncConnection

from review_panel.application import compute_draft, compute_or_get_draft
from review_panel.graph.build import STORE_NODE, build_panel_graph
from review_panel.infrastructure.postgres import REVIEW_PANEL_SCHEMA, to_psycopg_url
from review_panel.infrastructure.postgres_trace_sink import postgres_trace_sink
from review_panel.infrastructure.trace_sink import InMemoryTraceSink

EXPECTED_NODE_NAMES = {
    "load_pr",
    "review_bug",
    "review_security",
    "review_quality",
    "review_test_coverage",
    "reconcile",
    "store_draft",
}


# --------------------------------------------------------------------- hermetic (fake sink)


async def test_compute_draft_writes_one_root_span_and_one_per_node() -> None:
    pr = make_pr()
    sink = InMemoryTraceSink()
    deps, _, _, _ = make_deps(pr=pr, trace_sink=sink)

    await compute_draft(deps, InMemorySaver(), pr)

    names = {span.name for span in sink.spans}
    assert names == EXPECTED_NODE_NAMES | {"review_panel.draft_run"}
    assert all(span.service == "review-panel" for span in sink.spans)
    assert all(span.status == "ok" for span in sink.spans)
    assert {span.trace_id for span in sink.spans} == {key_of(pr)}  # one deterministic trace_id
    root = next(span for span in sink.spans if span.name == "review_panel.draft_run")
    assert root.parent_span_id is None
    node_parents = {
        span.parent_span_id for span in sink.spans if span.name != "review_panel.draft_run"
    }
    assert node_parents == {root.span_id}  # every node points at the root


async def test_a_raising_trace_sink_does_not_fail_the_draft_run() -> None:
    """Fail-soft boundary (ADR-0032 §3): a completely broken sink must never fail the
    draft run it observes."""
    pr = make_pr()
    deps, _, _, store = make_deps(pr=pr, trace_sink=RaisingTraceSink())

    outcome = await compute_draft(deps, InMemorySaver(), pr)

    assert outcome.draft.draft_key == key_of(pr)
    assert await store.get(key_of(pr)) == outcome.draft


async def test_null_trace_sink_is_the_default() -> None:
    pr = make_pr()
    deps, _, _, _ = make_deps(pr=pr)  # no trace_sink passed -> NullTraceSink default

    outcome = await compute_draft(deps, InMemorySaver(), pr)

    assert outcome.draft.draft_key == key_of(pr)  # ran to completion regardless


# -------------------------------------------------------------------------- crash-resume


async def test_crash_resume_never_re_emits_a_span_for_an_already_completed_node() -> None:
    pr = make_pr()
    sink = InMemoryTraceSink()
    deps, _, _, _ = make_deps(pr=pr, trace_sink=sink)
    saver = InMemorySaver()

    # killed before store_draft: load_pr + 4 reviewers + reconcile already ran
    crashed_run = build_panel_graph(deps, saver, interrupt_before=[STORE_NODE])
    await crashed_run.ainvoke(panel_input(pr), thread_config(pr))

    pre_resume = list(sink.spans)
    assert {span.name for span in pre_resume} == EXPECTED_NODE_NAMES - {"store_draft"}
    # a raw .ainvoke (simulating the killed process) never reaches compute_draft's
    # root-span wrapper — exactly like a real SIGKILL, whose in-flight root span is lost
    assert "review_panel.draft_run" not in {span.name for span in pre_resume}

    await compute_or_get_draft(deps, saver, pr.repo, pr.number)

    assert sink.spans[: len(pre_resume)] == pre_resume  # nothing already emitted is touched
    new_spans = sink.spans[len(pre_resume) :]
    assert {span.name for span in new_spans} == {"store_draft", "review_panel.draft_run"}


# --------------------------------------------------------------------------- Postgres-backed


@pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)
async def test_postgres_trace_sink_lands_rows_in_the_review_panel_schema() -> None:
    assert TEST_DATABASE_URL is not None
    pr = make_pr(head_sha=uuid.uuid4().hex + uuid.uuid4().hex[:8])
    deps, _, _, _ = make_deps(pr=pr)

    async with postgres_trace_sink(TEST_DATABASE_URL) as sink:
        deps_pg = dataclasses.replace(deps, trace_sink=sink)
        await compute_draft(deps_pg, InMemorySaver(), pr)

    async with await AsyncConnection.connect(to_psycopg_url(TEST_DATABASE_URL)) as conn:
        cursor = await conn.execute(
            f"SELECT name, service, status, parent_span_id, trace_id"
            f" FROM {REVIEW_PANEL_SCHEMA}.trace_span WHERE trace_id = %s ORDER BY started_at",
            (key_of(pr),),
        )
        rows = await cursor.fetchall()

    names = {row[0] for row in rows}
    assert names == EXPECTED_NODE_NAMES | {"review_panel.draft_run"}
    assert all(row[1] == "review-panel" for row in rows)
    assert all(row[2] == "ok" for row in rows)
    assert all(row[4] == key_of(pr) for row in rows)
