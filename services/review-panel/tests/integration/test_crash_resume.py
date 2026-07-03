"""The point of LangGraph here (ADR-0031 Consequences): a killed run resumes,
it never re-pays the four reviewer LLM calls.

Crash simulation: the first process is compiled with a breakpoint before
store_draft and stops there — reviewer nodes complete, nothing stored. The
"restarted process" resumes through the use-case (compute_or_get_draft) with a
FRESH compile sharing the same checkpointer and thread: it detects the pending
thread, resumes with input None, stores exactly ONE draft, and the model call
count stays flat."""

from collections import Counter

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from panel_test_support import (
    DEFAULT_RESPONSES,
    FakeModelClient,
    key_of,
    lens_of,
    make_deps,
    make_pr,
    panel_input,
    thread_config,
)

from review_panel.application import compute_or_get_draft
from review_panel.graph.build import STORE_NODE, build_panel_graph


async def test_kill_after_reviewers_then_resume_stores_exactly_one_draft() -> None:
    pr = make_pr()
    deps, model, _, store = make_deps(pr=pr)
    saver = InMemorySaver()

    crashed_run = build_panel_graph(deps, saver, interrupt_before=[STORE_NODE])
    await crashed_run.ainvoke(panel_input(pr), thread_config(pr))

    # killed after the reviewers completed, before storing
    assert len(model.calls) == 5  # 4 reviewers + synthesizer all done
    assert await store.get(key_of(pr)) is None

    outcome = await compute_or_get_draft(deps, saver, pr.repo, pr.number)

    assert outcome.source == "resumed"
    assert outcome.draft.draft_key == key_of(pr)
    assert await store.get(key_of(pr)) == outcome.draft  # exactly one draft landed
    # resume did NOT re-run load_pr or any reviewer: zero new model calls
    assert len(model.calls) == 5


async def test_mid_fanout_crash_never_reinvokes_the_completed_reviewers() -> None:
    """Killed MID-fan-out (2 of 4 reviewers done), not at a node boundary.

    An in-process test cannot SIGKILL itself, so the closest faithful stand-in
    is two reviewer tasks raising inside the fan-out superstep: LangGraph
    persists the successful siblings' results as checkpoint pending-writes
    before the superstep fails, exactly what a killed process leaves behind.
    On resume, tasks with recorded writes are skipped — the completed
    reviewers' LLM spend is never re-paid; only the unfinished ones re-run.
    """
    pr = make_pr()
    attempts: Counter[str] = Counter()
    failing = {"quality", "test_coverage"}

    def respond(system: str, user: str) -> str:
        lens = lens_of(system)
        attempts[lens] += 1
        if lens in failing and attempts[lens] == 1:
            raise RuntimeError("simulated mid-fan-out crash")
        return DEFAULT_RESPONSES[lens]

    deps, _, _, store = make_deps(pr=pr, model=FakeModelClient(respond=respond))
    saver = InMemorySaver()

    crashed_run = build_panel_graph(deps, saver)
    with pytest.raises(RuntimeError, match="simulated mid-fan-out crash"):
        await crashed_run.ainvoke(panel_input(pr), thread_config(pr))

    completed_before_crash = {lens for lens in ("bug", "security") if attempts[lens] == 1}
    assert completed_before_crash == {"bug", "security"}  # 2 of 4 reviewers finished
    assert await store.get(key_of(pr)) is None  # crashed before reconcile/store

    outcome = await compute_or_get_draft(deps, saver, pr.repo, pr.number)

    assert outcome.source == "resumed"
    # the completed reviewers were NOT re-invoked on resume ...
    assert attempts["bug"] == 1
    assert attempts["security"] == 1
    # ... only the unfinished ones re-ran, then the synthesizer, exactly once
    assert attempts["quality"] == 2
    assert attempts["test_coverage"] == 2
    assert attempts["synthesizer"] == 1
    # reconcile saw exactly one review per lens (no duplicates from the resume)
    snapshot = await build_panel_graph(deps, saver).aget_state(thread_config(pr))
    assert len(snapshot.values["panelist_reviews"]) == 4
    assert await store.get(key_of(pr)) == outcome.draft


async def test_second_resume_of_a_finished_thread_reuses_the_stored_draft() -> None:
    """After the resume completes, another run is a pure store hit — no graph work."""
    pr = make_pr()
    deps, model, _, store = make_deps(pr=pr)
    saver = InMemorySaver()

    crashed_run = build_panel_graph(deps, saver, interrupt_before=[STORE_NODE])
    await crashed_run.ainvoke(panel_input(pr), thread_config(pr))
    first = await compute_or_get_draft(deps, saver, pr.repo, pr.number)
    assert first.source == "resumed"

    second = await compute_or_get_draft(deps, saver, pr.repo, pr.number)
    assert second.source == "stored"
    assert second.draft == first.draft
    assert len(model.calls) == 5  # still zero additional model spend
    assert await store.get(key_of(pr)) == first.draft
