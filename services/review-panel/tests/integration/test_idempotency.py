"""Same-head_sha re-runs return the stored draft with zero recompute; a new
head SHA is a new key and a fresh computation; a racing store keeps one row."""

from langgraph.checkpoint.memory import InMemorySaver
from panel_test_support import (
    FakeGitHubClient,
    key_of,
    make_deps,
    make_pr,
    panel_input,
    thread_config,
)

from review_panel.application import compute_or_get_draft
from review_panel.graph.build import build_panel_graph


async def test_rerun_on_same_head_sha_returns_stored_draft_without_recompute() -> None:
    pr = make_pr()
    deps, model, _, store = make_deps(pr=pr)
    first = await compute_or_get_draft(deps, InMemorySaver(), pr.repo, pr.number)
    assert first.source == "computed"
    assert len(model.calls) == 5

    # a totally fresh re-run (new checkpointer — e.g. a new process with an
    # empty memory saver): the DRAFT STORE, not the checkpoint, is the guard
    second = await compute_or_get_draft(deps, InMemorySaver(), pr.repo, pr.number)

    assert second.source == "stored"
    assert second.draft == first.draft
    assert len(model.calls) == 5  # zero additional model spend
    assert await store.get(key_of(pr)) == first.draft  # still exactly one draft


async def test_new_head_sha_gets_a_new_draft() -> None:
    pr_v1 = make_pr(head_sha="a" * 40)
    deps, model, github, store = make_deps(pr=pr_v1)
    v1 = await compute_or_get_draft(deps, InMemorySaver(), pr_v1.repo, pr_v1.number)
    assert v1.source == "computed"
    assert len(model.calls) == 5

    pr_v2 = make_pr(head_sha="b" * 40)
    github.pr = pr_v2  # same PR, new commit
    v2 = await compute_or_get_draft(deps, InMemorySaver(), pr_v2.repo, pr_v2.number)

    assert v2.source == "computed"  # new key => fresh review
    assert len(model.calls) == 10
    assert v1.draft.draft_key != v2.draft.draft_key
    assert await store.get(key_of(pr_v1)) == v1.draft
    assert await store.get(key_of(pr_v2)) == v2.draft


async def test_deleted_draft_row_recomputes_clean_without_doubling_reviews() -> None:
    """A COMPLETED checkpoint thread whose draft row was deleted out-of-band must
    recompute from CLEAN state. Re-invoking fresh input on the finished thread
    would MERGE into the checkpointed reducer state — operator.add appends four
    MORE panelist_reviews, feeding eight into reconcile."""
    pr = make_pr()
    deps, model, _, store = make_deps(pr=pr)
    saver = InMemorySaver()
    first = await compute_or_get_draft(deps, saver, pr.repo, pr.number)
    assert first.source == "computed"
    assert len(model.calls) == 5

    # manual cleanup / operator error: the draft row vanishes, the thread remains
    store._drafts.pop(key_of(pr))

    second = await compute_or_get_draft(deps, saver, pr.repo, pr.number)

    assert second.source == "computed"  # an honest fresh recompute, not a fake resume
    assert len(model.calls) == 10
    assert await store.get(key_of(pr)) == second.draft
    # the recomputed thread carries exactly ONE review per lens — never eight
    snapshot = await build_panel_graph(deps, saver).aget_state(thread_config(pr))
    assert len(snapshot.values["panelist_reviews"]) == 4


async def test_racing_runs_on_the_same_sha_keep_exactly_one_draft() -> None:
    """Two concurrent engines both compute (neither saw a stored draft); the
    store's put_if_absent keeps the first row and hands it to the loser."""
    pr = make_pr()
    deps_a, _, _, shared_store = make_deps(pr=pr)
    graph_a = build_panel_graph(deps_a, InMemorySaver())
    result_a = await graph_a.ainvoke(panel_input(pr), thread_config(pr))
    draft_a = result_a["draft"]

    # rival run raced past the lookup: separate checkpointer, same shared store
    deps_b, model_b, _, _ = make_deps(pr=pr, github=FakeGitHubClient(pr), store=shared_store)
    graph_b = build_panel_graph(deps_b, InMemorySaver())
    result_b = await graph_b.ainvoke(panel_input(pr), thread_config(pr))

    assert len(model_b.calls) == 5  # the rival did review (it had already started)...
    assert result_b["draft"] == draft_a  # ...but the stored winner is A's draft
    assert await shared_store.get(key_of(pr)) == draft_a  # exactly one row survives
