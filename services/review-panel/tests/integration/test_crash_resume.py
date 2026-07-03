"""The point of LangGraph here (ADR-0031 Consequences): a killed run resumes,
it never re-pays the four reviewer LLM calls.

Crash simulation: the first process is compiled with a breakpoint before
store_draft and stops there — reviewer nodes complete, nothing stored. The
"restarted process" resumes through the use-case (compute_or_get_draft) with a
FRESH compile sharing the same checkpointer and thread: it detects the pending
thread, resumes with input None, stores exactly ONE draft, and the model call
count stays flat."""

from langgraph.checkpoint.memory import InMemorySaver
from panel_test_support import key_of, make_deps, make_pr, panel_input, thread_config

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
